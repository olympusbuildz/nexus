"""ChatGPT plan backend: the Codex Responses API, signed in with the Codex CLI login.

Same plan, same endpoint and same login as ``codex`` itself, but spoken to directly
over HTTP, so it keeps what the CLI backend cannot: function tools, real token
streaming, and images. Chat completions are translated to Responses and back in
``nexus.chatgpt_responses``. The access token is read from the Codex login file at
request time (``nexus.chatgpt_auth``) and is never logged, stored elsewhere or returned.

At your own risk: this uses a ChatGPT subscription through an endpoint built for the
Codex clients. Check OpenAI's terms for your plan before you turn it on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nexus import chatgpt_auth
from nexus.backends.base import ChatUpstream
from nexus.backends.upstream import sse_error, ssl_failure
from nexus.chatgpt_responses import (
    ChatSseFromResponses,
    ResponsesFailed,
    chat_to_responses,
    completion_from_events,
    failure_detail,
    parse_sse,
)
from nexus.config import environ_get, health_timeout, model_for, upstream_timeout
from nexus.errors import GatewayError
from nexus.models_meta import NO_UPSTREAM, unverified
from nexus.redact import redact

logger = logging.getLogger("nexus")

DEFAULT_BASE = "https://chatgpt.com/backend-api/codex"
# The models endpoint wants a Codex client version; it gates which models it lists.
DEFAULT_CLIENT_VERSION = "0.162.1"
CATALOG = ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol")
_MODELS_TTL = 300.0
_models_cache: dict[str, tuple[float, list[str]]] = {}
# Seen live: ~3% of opens die in the TLS handshake with nothing sent back, and a blip
# can last about a second, so one quick retry is not always enough. Up to two retries,
# the second after a longer pause; an HTTP status is an answer, never retried here.
_OPEN_RETRY_BACKOFF_S: tuple[tuple[float, float], ...] = ((0.3, 0.5), (1.0, 1.5))
_TRANSIENT_OPEN_ERRORS = (
    ssl.SSLError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
)


def chatgpt_base() -> str:
    return ((environ_get("NEXUS_CHATGPT_BASE") or "").strip() or DEFAULT_BASE).rstrip("/")


def client_version() -> str:
    return (environ_get("NEXUS_CHATGPT_CLIENT_VERSION") or "").strip() or DEFAULT_CLIENT_VERSION


def _headers(creds: chatgpt_auth.Credentials, *, stream: bool) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "chatgpt-account-id": creds.account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "session_id": str(uuid.uuid4()),
    }
    if stream:
        headers["accept"] = "text/event-stream"
        headers["content-type"] = "application/json"
    return headers


class ChatGPTBackend:
    id = "chatgpt"
    label = "ChatGPT plan"
    credential_check_first = False

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport, follow_redirects=False)

    def describe(self) -> dict[str, Any]:
        login, hint = chatgpt_auth.login_status()
        ready = login == "ok"
        cached = _models_cache.get(self.id)
        models = list(cached[1]) if cached else list(dict.fromkeys((model_for(self.id), *CATALOG)))
        return {
            "id": self.id,
            "name": self.label,
            "kind": "rest",
            "role": "subscription",
            "implemented": True,
            "ready": ready,
            "key_present": ready,
            "key_source": "codex-login" if ready else None,
            "key_file": str(chatgpt_auth.auth_path()),
            "key_mode": None,
            "credential": "cli-login",
            "login": login,
            "base": chatgpt_base(),
            "default_model": model_for(self.id),
            "hint": hint,
            "models": models,
            "models_source": "account" if cached else "catalog",
            "models_hint": (
                "Listed by the ChatGPT Codex models endpoint for this login."
                if cached
                else "the bridge's own catalog. GET /v1/models lists what this ChatGPT plan offers."
            ),
        }

    async def _model_ids(self, timeout: float) -> list[str] | None:
        cached = _models_cache.get(self.id)
        if cached is not None and time.monotonic() - cached[0] < _MODELS_TTL:
            return list(cached[1])
        try:
            creds = await chatgpt_auth.access()
        except GatewayError:
            return None
        try:
            async with self._client(timeout) as client:
                response = await client.get(
                    f"{chatgpt_base()}/models",
                    params={"client_version": client_version()},
                    headers=_headers(creds, stream=False),
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        rows = payload.get("models") if isinstance(payload, dict) else payload
        ids: list[str] = []
        listed: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            slug = row.get("slug") or row.get("id")
            if not isinstance(slug, str) or not slug or slug in ids:
                continue
            ids.append(slug)
            if row.get("visibility", "list") == "list":
                listed.append(slug)
        if not ids:
            return None
        # Models the picker shows first, then the hidden ones the plan still serves.
        ordered = listed + [m for m in ids if m not in listed]
        _models_cache[self.id] = (time.monotonic(), ordered)
        return list(ordered)

    async def model_reachable(self, model: str) -> bool | None:
        if chatgpt_auth.login_status()[0] != "ok":
            return None
        ids = await self._model_ids(health_timeout())
        if ids is None:
            return None
        return model in ids

    async def credential_check(self) -> dict[str, Any] | None:
        return None

    async def list_models(self) -> list[dict[str, Any]]:
        now = int(time.time())
        ids = await self._model_ids(min(20.0, upstream_timeout()))
        if ids is None:
            return [
                unverified(m, now, owned_by="openai", reason=NO_UPSTREAM)
                for m in dict.fromkeys((model_for(self.id), *CATALOG))
            ]
        preferred = model_for(self.id)
        if preferred in ids:
            ids = [preferred, *[m for m in ids if m != preferred]]
        return [{"id": m, "object": "model", "created": now, "owned_by": "openai"} for m in ids]

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        model = str(payload.get("model") or model_for(self.id))
        body = chat_to_responses(payload, model=model)
        response, close = await self._open(body)
        if stream:
            return ChatUpstream(
                status_code=200,
                media_type="text/event-stream",
                stream=_chat_sse(response, close, model),
            )
        try:
            events: list[dict[str, Any]] = []
            buffer = b""
            async for chunk in response.aiter_bytes():
                parsed, buffer = parse_sse(buffer + chunk)
                events.extend(parsed)
            parsed, _ = parse_sse(buffer + b"\n\n")
            events.extend(parsed)
        except httpx.TimeoutException:
            raise _timeout() from None
        except ssl.SSLError:
            raise ssl_failure() from None
        except httpx.HTTPError:
            raise _unreachable() from None
        finally:
            await close()
        try:
            completion = completion_from_events(events, model=model)
        except ResponsesFailed as failed:
            raise _failed(failed.event) from None
        raw = json.dumps(completion, ensure_ascii=False).encode("utf-8")
        return ChatUpstream(status_code=200, media_type="application/json", body=raw)

    async def _open(self, body: dict[str, Any]) -> tuple[httpx.Response, Any]:
        """POST /responses with one forced refresh on 401. Returns (response, close)."""
        url = f"{chatgpt_base()}/responses"
        retries = 0
        for attempt in (0, 1):
            creds = await chatgpt_auth.access(force_refresh=attempt == 1)
            while True:
                client = self._client(upstream_timeout())
                try:
                    opener = client.stream(
                        "POST", url, headers=_headers(creds, stream=True), json=body
                    )
                    response = await opener.__aenter__()
                except (httpx.HTTPError, ssl.SSLError) as exc:
                    await client.aclose()
                    transient = isinstance(exc, _TRANSIENT_OPEN_ERRORS)
                    if transient and retries < len(_OPEN_RETRY_BACKOFF_S):
                        pause = random.uniform(*_OPEN_RETRY_BACKOFF_S[retries])
                        retries += 1
                        logger.warning(
                            "chatgpt open failed (%s); retry %d of %d",
                            type(exc).__name__,
                            retries,
                            len(_OPEN_RETRY_BACKOFF_S),
                        )
                        await asyncio.sleep(pause)
                        continue
                    if transient:
                        logger.warning(
                            "chatgpt open retry did not recover after %d retries (%s)",
                            retries,
                            type(exc).__name__,
                        )
                    raise _open_failure(exc) from None
                if retries:
                    logger.info("chatgpt open retry recovered after %d retries", retries)
                break

            async def close(opener: Any = opener, client: httpx.AsyncClient = client) -> None:
                await opener.__aexit__(None, None, None)
                await client.aclose()

            _note_usage(response.headers)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await close()
                continue
            if response.status_code >= 300:
                raw = await response.aread()
                await close()
                raise _http_error(response, raw, creds)
            return response, close
        raise AssertionError("unreachable")  # pragma: no cover


async def _chat_sse(response: httpx.Response, close: Any, model: str) -> AsyncIterator[bytes]:
    translator = ChatSseFromResponses(model)
    try:
        try:
            async for chunk in response.aiter_bytes():
                for line in translator.feed(chunk):
                    yield line
            for line in translator.feed(b"\n\n"):
                yield line
        except ResponsesFailed as failed:
            yield sse_error(_failed(failed.event))
        except ssl.SSLError:
            logger.warning("upstream SSL error mid-stream backend=chatgpt")
            yield sse_error(ssl_failure())
        except httpx.HTTPError:
            yield sse_error(_unreachable())
        else:
            if not translator.done:
                yield sse_error(
                    GatewayError(
                        502,
                        "ChatGPT stream ended before the response completed",
                        "upstream_error",
                        "chatgpt_incomplete_stream",
                    )
                )
        yield b"data: [DONE]\n\n"
    finally:
        await close()


def _extra(status: int, text: str) -> dict[str, Any]:
    detail: dict[str, Any] = {"upstream_status": status}
    if text:
        detail["upstream_body"] = text[:4000]
    return {"chatgpt": detail, "upstream": {"backend": "chatgpt", **detail}}


def _http_error(
    response: httpx.Response, raw: bytes, creds: chatgpt_auth.Credentials
) -> GatewayError:
    status = response.status_code
    text = redact(raw.decode("utf-8", errors="replace"), [creds.access_token, creds.account_id])
    retry = response.headers.get("retry-after")
    try:
        retry_after = float(retry) if retry else None
    except ValueError:
        retry_after = None
    if status in {401, 403}:
        return GatewayError(
            502,
            f"ChatGPT refused the Codex login (upstream HTTP {status}).",
            "upstream_error",
            "chatgpt_auth_rejected",
            extra={**_extra(status, text), "hint": chatgpt_auth.LOGIN_HINT},
        )
    if status == 429 or "usage_limit" in text:
        return GatewayError(
            429,
            "ChatGPT plan usage limit reached. Wait for the window to reset, or switch backend.",
            "rate_limit_error",
            "chatgpt_quota",
            extra=_extra(429, text),
            retry_after=retry_after,
        )
    if status == 400 and ("model" in text.lower() and "support" in text.lower()):
        return GatewayError(
            400,
            "This ChatGPT plan does not serve that model. GET /v1/models lists the ones it does.",
            "invalid_request_error",
            "chatgpt_model_not_supported",
            extra=_extra(status, text),
        )
    return GatewayError(
        502 if status >= 500 or status < 400 else status,
        f"ChatGPT upstream error (HTTP {status})",
        "upstream_error",
        "chatgpt_upstream_error",
        extra=_extra(status, text),
        retry_after=retry_after,
    )


def _failed(event: dict[str, Any]) -> GatewayError:
    code, message = failure_detail(event)
    if "usage_limit" in code or "rate_limit" in code:
        return GatewayError(
            429, message, "rate_limit_error", "chatgpt_quota", extra=_extra(429, code)
        )
    return GatewayError(502, message, "upstream_error", "chatgpt_response_failed")


def _open_failure(exc: Exception) -> GatewayError:
    if isinstance(exc, httpx.TimeoutException):
        return _timeout()
    if isinstance(exc, ssl.SSLError):
        return ssl_failure()
    return _unreachable()


def _timeout() -> GatewayError:
    return GatewayError(504, "ChatGPT request timed out", "upstream_error", "chatgpt_timeout")


def _unreachable() -> GatewayError:
    return GatewayError(502, "ChatGPT unreachable", "upstream_error", "chatgpt_unreachable")


def _note_usage(headers: Any) -> None:
    """Keep the plan's x-codex-* usage numbers for /dashboard/usage. Never raises."""
    try:
        from nexus.usage import store

        store().record_headers("chatgpt", headers)
    except Exception:
        pass


def usage_url() -> str:
    """The plan's own usage endpoint, the one the Codex CLI's ``/status`` reads."""
    override = (environ_get("NEXUS_CHATGPT_USAGE_URL") or "").strip()
    if override:
        return override
    base = chatgpt_base()
    root = base[: -len("/codex")] if base.endswith("/codex") else base
    return f"{root}/wham/usage"


async def fetch_plan_usage(
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """(parsed windows, None) from ``GET /wham/usage``, or (None, a printable note).

    Read-only: no chat is sent and nothing is spent. The body names the user, account
    and email; only the numbers ``usage.parse_codex_usage`` allows are kept. Never raises.
    """
    from nexus.usage import parse_codex_usage

    try:
        creds = await chatgpt_auth.access()
    except Exception:
        return None, "ChatGPT login unavailable."
    try:
        async with httpx.AsyncClient(
            timeout=health_timeout(), transport=transport, trust_env=False
        ) as client:
            response = await client.get(usage_url(), headers=_headers(creds, stream=False))
    except httpx.HTTPError:
        return None, "ChatGPT usage endpoint could not be reached."
    if response.status_code != 200:
        return None, f"ChatGPT usage endpoint answered HTTP {response.status_code}."
    try:
        parsed = parse_codex_usage(response.json(), time.time())
    except ValueError:
        parsed = None
    if parsed is None:
        return None, "ChatGPT usage endpoint sent no windows."
    return parsed, None
