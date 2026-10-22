"""Anthropic native Messages API. Chat completions are translated. Key never logged."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from nexus.anthropic_chat import (
    OpenAISseFromAnthropic,
    anthropic_message_to_completion,
    openai_chat_to_anthropic,
)
from nexus.backends.base import ChatUpstream
from nexus.backends.upstream import Upstream, credential_rejection, describe_upstream_failure
from nexus.config import (
    anthropic_base,
    anthropic_key_path,
    health_timeout,
    model_for,
    upstream_timeout,
)
from nexus.errors import GatewayError
from nexus.models_meta import NO_UPSTREAM, NOT_LISTED, unverified

ANTHROPIC_VERSION = "2023-06-01"
FALLBACK_MODELS = (
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4-1",
)


def _headers(key: str) -> dict[str, str]:
    return {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


class AnthropicBackend:
    id = "anthropic"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = anthropic_base
        self._http = Upstream(
            backend_id="anthropic",
            label="Anthropic",
            env_var="ANTHROPIC_API_KEY",
            key_path=anthropic_key_path,
            headers=_headers,
            transport=transport,
        )

    def describe(self) -> dict[str, Any]:
        status = self._http.readiness(
            role="optional",
            base=self._base_url(),
            model=model_for(self.id),
        )
        status["api"] = "messages"
        catalog = _dedupe_catalog((model_for(self.id), *FALLBACK_MODELS))
        status["models"] = list(catalog)
        status["models_source"] = "catalog"
        status["models_hint"] = (
            "the bridge's own catalog. GET /v1/models lists what Anthropic really "
            "offers for this key."
        )
        return status

    credential_check_first = False

    async def credential_check(self) -> dict[str, Any] | None:
        """A rejection verdict when Anthropic refuses the key, else None."""
        if self._http.load() is None:
            return None
        try:
            status, body = await self._http.get(
                f"{self._base_url()}/v1/models", timeout=health_timeout()
            )
        except GatewayError:
            return None
        if status in {401, 403}:
            return credential_rejection(self._http.label, status, body)
        return None

    async def model_reachable(self, model: str) -> bool | None:
        """True/False when the Anthropic /v1/models list answers, else None."""
        if self._http.load() is None:
            return None
        try:
            status, body = await self._http.get(
                f"{self._base_url()}/v1/models",
                timeout=health_timeout(),
            )
        except GatewayError:
            return None
        if status != 200:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        return model in {row.get("id") for row in data if isinstance(row, dict)}

    async def list_models(self) -> list[dict[str, Any]]:
        now = int(time.time())
        fallback = _curated(now)
        if self._http.load() is None:
            return fallback
        try:
            status, body = await self._http.get(
                f"{self._base_url()}/v1/models",
                timeout=min(20.0, upstream_timeout()),
            )
        except GatewayError:
            return fallback
        if status != 200:
            return fallback
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return fallback
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return fallback
        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
                continue
            seen.add(model_id)
            parsed.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": _created(item.get("created_at"), now),
                    "owned_by": "anthropic",
                }
            )
        preferred = model_for(self.id)
        if not parsed:
            return fallback
        if preferred in seen:
            first = next(row for row in parsed if row["id"] == preferred)
            return [first, *[row for row in parsed if row["id"] != preferred]]
        return [
            unverified(preferred, now, reason=NOT_LISTED),
            *parsed,
        ]

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        body = openai_chat_to_anthropic(payload)
        body["stream"] = stream
        url = f"{self._base_url()}/v1/messages"
        try:
            if stream:
                upstream = await self._http.open_stream(url, body)
            else:
                upstream = await self._http.complete(url, body)
        except GatewayError as exc:
            raise _name_model(exc, self._http, str(body["model"])) from None
        return _as_openai_chat(upstream, fallback_model=str(body["model"]))

    async def open_messages(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        """Native Messages. The body is forwarded; this is not the chat translator."""
        body = dict(payload)
        body["stream"] = stream
        url = f"{self._base_url()}/v1/messages"
        if stream:
            return await self._http.open_stream(url, body)
        return await self._http.complete(url, body)


def _as_openai_chat(upstream: ChatUpstream, *, fallback_model: str) -> ChatUpstream:
    if upstream.stream is not None:
        translator = OpenAISseFromAnthropic(fallback_model)
        agen = upstream.stream

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in agen:
                    for event in translator.feed(chunk):
                        yield event
                for event in translator.finish():
                    yield event
            finally:
                await agen.aclose()

        return ChatUpstream(
            status_code=upstream.status_code,
            media_type="text/event-stream",
            stream=chunks(),
        )
    try:
        message = json.loads((upstream.body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError(
            502,
            "Anthropic returned an unusable message",
            "upstream_error",
            "anthropic_bad_body",
        ) from None
    completion = anthropic_message_to_completion(message, fallback_model=fallback_model)
    raw = json.dumps(completion, ensure_ascii=False).encode("utf-8")
    return ChatUpstream(status_code=upstream.status_code, media_type="application/json", body=raw)


def _dedupe_catalog(ids: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for model_id in ids:
        if model_id and model_id not in seen:
            seen.add(model_id)
            out.append(model_id)
    return tuple(out)


def _curated(now: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model_id in (model_for("anthropic"), *FALLBACK_MODELS):
        if model_id in seen:
            continue
        seen.add(model_id)
        rows.append(unverified(model_id, now, reason=NO_UPSTREAM))
    return rows


def _created(value: object, now: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            return now
    return now


def _name_model(exc: GatewayError, http: Upstream, model: str) -> GatewayError:
    return describe_upstream_failure(exc, backend_id=http.backend_id, label=http.label, model=model)
