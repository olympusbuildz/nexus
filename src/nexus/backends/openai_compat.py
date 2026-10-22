"""OpenAI-compatible chat completions and /models. Key never logged."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from nexus.backends.base import ChatUpstream
from nexus.backends.upstream import Upstream, credential_rejection, describe_upstream_failure
from nexus.config import health_timeout, model_for, upstream_timeout
from nexus.errors import GatewayError
from nexus.models_meta import NO_UPSTREAM, NOT_LISTED, unverified


def _dedupe(ids: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for model_id in ids:
        if model_id and model_id not in seen:
            seen.add(model_id)
            out.append(model_id)
    return tuple(out)


class OpenAICompatibleBackend:
    def __init__(
        self,
        *,
        backend_id: str,
        role: str,
        label: str,
        owned_by: str,
        env_var: str,
        key_path: Callable[[], Path],
        base_url: Callable[[], str],
        fallback_models: tuple[str, ...],
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        credential_path: str | None = None,
    ) -> None:
        self.id = backend_id
        # None: GET /models is the auth check. A provider whose /models catalog is
        # public even for a dead key names a credential-bound path instead.
        self._credential_path = credential_path
        self._role = role
        self._owned_by = owned_by
        self._base_url = base_url
        self._fallback_models = fallback_models
        headers = dict(extra_headers or {})

        def _headers(key: str) -> dict[str, str]:
            return {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **headers,
            }

        self._http = Upstream(
            backend_id=backend_id,
            label=label,
            env_var=env_var,
            key_path=key_path,
            headers=_headers,
            transport=transport,
        )

    def describe(self) -> dict[str, Any]:
        row = self._http.readiness(
            role=self._role,
            base=self._base_url(),
            model=model_for(self.id),
        )
        catalog = _dedupe((model_for(self.id), *self._fallback_models))
        row["models"] = list(catalog)
        row["models_source"] = "catalog"
        row["models_hint"] = (
            f"the bridge's own catalog. GET /v1/models lists what {self._http.label} "
            "really offers for this key."
        )
        return row

    @property
    def credential_check_first(self) -> bool:
        """True when /models cannot vouch for the key, so the check must run before it."""
        return self._credential_path is not None

    async def credential_check(self) -> dict[str, Any] | None:
        """A verdict when the key is not known good, else None.

        Refused (401/403) is ``ready: False`` with a hint. When /models cannot vouch for the
        key, a check that could not be completed is ``ready: None``: unverified,
        never a green. Otherwise an unanswered check proves nothing and /models
        gets the say.
        """
        if self._http.load() is None:
            return None
        try:
            status, body = await self._http.get(
                f"{self._base_url()}{self._credential_path or '/models'}",
                timeout=health_timeout(),
            )
        except GatewayError:
            return {"ready": None} if self._credential_path else None
        if status in {401, 403}:
            return credential_rejection(self._http.label, status, body)
        if self._credential_path and not 200 <= status < 300:
            return {"ready": None, "upstream_status": status}
        return None

    async def model_reachable(self, model: str) -> bool | None:
        """True/False when /models answers, None when it cannot be told.

        An OpenAI-compatible /models list is the cheapest live check there is:
        a model id absent from it is a model id that will 404 on chat.
        """
        if self._http.load() is None:
            return None
        try:
            status, body = await self._http.get(
                f"{self._base_url()}/models", timeout=health_timeout()
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
        ids = {row.get("id") for row in data if isinstance(row, dict)}
        return model in ids

    async def accepts_images(self, model: str) -> bool | None:
        """Whether ``model`` takes image input, from /models ``architecture``. None: unknown."""
        if self._http.load() is None:
            return None
        try:
            status, body = await self._http.get(
                f"{self._base_url()}/models", timeout=health_timeout()
            )
            payload = json.loads(body.decode("utf-8")) if status == 200 else None
        except (GatewayError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict) and row.get("id") == model:
                arch = row.get("architecture")
                mods = arch.get("input_modalities") if isinstance(arch, dict) else None
                if isinstance(mods, list):
                    return "image" in mods
                modality = arch.get("modality") if isinstance(arch, dict) else None
                if isinstance(modality, str) and "->" in modality:
                    return "image" in modality.split("->", 1)[0]
                return None
        return False

    async def list_models(self) -> list[dict[str, Any]]:
        now = int(time.time())
        fallback = self._curated(now)
        if self._http.load() is None:
            return fallback
        try:
            status, body = await self._http.get(
                f"{self._base_url()}/models",
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
            row = _public_model(item, now=now, owned_by=self._owned_by)
            if row is None or row["id"] in seen:
                continue
            seen.add(row["id"])
            parsed.append(row)
        preferred = model_for(self.id)
        if preferred in seen:
            first = next(row for row in parsed if row["id"] == preferred)
            return [first, *[row for row in parsed if row["id"] != preferred]]
        return [_unverified(preferred, now), *parsed]

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        url = f"{self._base_url()}/chat/completions"
        try:
            if stream:
                return await self._http.open_stream(url, stream_payload(payload))
            return await self._http.complete(url, payload)
        except GatewayError as exc:
            raise self._name_model(exc, payload) from None

    def _name_model(self, exc: GatewayError, payload: dict[str, Any]) -> GatewayError:
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            return exc
        return describe_upstream_failure(
            exc, backend_id=self._http.backend_id, label=self._http.label, model=model.strip()
        )

    def _curated(self, now: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for model_id in (model_for(self.id), *self._fallback_models):
            if model_id in seen:
                continue
            seen.add(model_id)
            rows.append(_curated_row(model_id, now))
        return rows


def _public_model(raw: dict[str, Any], *, now: int, owned_by: str) -> dict[str, Any] | None:
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    created = raw.get("created")
    owner = raw.get("owned_by")
    return {
        "id": model_id,
        "object": "model",
        "created": created if isinstance(created, int) else now,
        "owned_by": owner if isinstance(owner, str) and owner else owned_by,
    }


def _unverified(model_id: str, now: int) -> dict[str, Any]:
    """A row the upstream list did not confirm. Never a silent row."""
    return unverified(model_id, now, reason=NOT_LISTED)


def _curated_row(model_id: str, now: int) -> dict[str, Any]:
    """A row from the offline catalog: nothing here was confirmed."""
    return unverified(model_id, now, reason=NO_UPSTREAM)


def stream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask for the provider's own token counts on a stream (``include_usage``).

    Nexus drops the extra usage-only chunk again when the client did not ask for it.
    ``NEXUS_STREAM_USAGE=0`` turns this off for a provider that rejects the option.
    """
    from nexus.config import environ_get
    from nexus.usage_sniff import with_stream_usage

    if (environ_get("NEXUS_STREAM_USAGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return payload
    return with_stream_usage(payload)
