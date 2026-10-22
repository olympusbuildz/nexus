"""Shared upstream HTTP. trust_env is off and redirects are refused.

The Authorization or x-api-key value is redacted from any body we return.
It is not written to the log.
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from nexus.backends.base import ChatUpstream
from nexus.config import private_mode, upstream_timeout
from nexus.errors import GatewayError
from nexus.keys import key_source, load_key
from nexus.redact import redact, redact_bytes

logger = logging.getLogger("nexus")


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (delta or HTTP date), or None."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(text) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, seconds) if seconds == seconds else None


def rejection_detail(body: bytes) -> str:
    """Short upstream reason for a rejected key. Body is already redacted."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "credential rejected"
    if not isinstance(payload, dict):
        return "credential rejected"
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:200]
    if isinstance(error, str) and error.strip():
        return error.strip()[:200]
    return "credential rejected"


def credential_rejection(label: str, status: int, body: bytes) -> dict[str, object]:
    """The /health verdict for a key the provider refused (401/403)."""
    return {
        "ready": False,
        "upstream_status": status,
        "hint": (
            f"{label} rejected the API key (upstream HTTP {status}): {rejection_detail(body)}. "
            "Replace it and re-check with ./doctor.sh"
        ),
    }


def ssl_failure() -> GatewayError:
    """A TLS failure talking to the provider: a clean 502, not a raw 500."""
    return GatewayError(502, "upstream SSL error", "upstream_ssl_error", "upstream_ssl_error")


def sse_error(exc: GatewayError) -> bytes:
    """One OpenAI-style SSE error event, for a failure after the 200 was sent."""
    body = {"error": {"message": exc.message, "type": exc.err_type, "code": exc.code}}
    return b"data: " + json.dumps(body).encode() + b"\n\n"


class Upstream:
    def __init__(
        self,
        *,
        backend_id: str,
        label: str,
        env_var: str,
        key_path: Callable[[], Path],
        headers: Callable[[str], dict[str, str]],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.backend_id = backend_id
        self.label = label
        self.env_var = env_var
        self.key_path = key_path
        self._headers = headers
        self._transport = transport

    def load(self) -> str | None:
        return load_key(env_var=self.env_var, path=self.key_path())

    def source(self) -> str | None:
        return key_source(env_var=self.env_var, path=self.key_path())

    def readiness(
        self,
        *,
        role: str,
        base: str,
        model: str,
    ) -> dict[str, Any]:
        source = self.source()
        path = self.key_path()
        ready = source is not None
        if ready:
            hint = "ok"
        else:
            hint = (
                f"{self.label} key missing. Set {self.env_var} or create a mode-600 file at {path}"
            )
        return {
            "id": self.backend_id,
            "name": self.label,
            "kind": "rest",
            "role": role,
            "implemented": True,
            "ready": ready,
            "key_present": ready,
            "key_source": source,
            "key_file": str(path),
            "key_mode": private_mode(path),
            "base": base,
            "default_model": model,
            "hint": hint,
        }

    def require_key(self) -> str:
        key = self.load()
        if not key:
            raise self.missing_key()
        return key

    def missing_key(self) -> GatewayError:
        path = self.key_path()
        detail = {"key_present": False, "key_file": str(path)}
        return GatewayError(
            503,
            f"{self.label} API key not configured",
            f"{self.backend_id}_unavailable",
            f"{self.backend_id}_key_missing",
            extra={self.backend_id: detail, "upstream": {"backend": self.backend_id, **detail}},
        )

    def client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(upstream_timeout() if timeout is None else timeout),
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def get(self, url: str, *, timeout: float) -> tuple[int, bytes]:
        key = self.require_key()
        try:
            async with self.client(timeout) as client:
                response = await client.get(url, headers=self._headers(key))
        except ssl.SSLError:
            raise self._unreachable(ssl_error=True) from None
        except httpx.HTTPError:
            raise self._unreachable() from None
        return response.status_code, redact_bytes(response.content, [key])

    async def complete(self, url: str, payload: dict[str, Any]) -> ChatUpstream:
        key = self.require_key()
        try:
            async with self.client() as client:
                response = await client.post(url, headers=self._headers(key), json=payload)
        except httpx.TimeoutException:
            raise self._timeout() from None
        except ssl.SSLError:
            raise self._unreachable(ssl_error=True) from None
        except httpx.HTTPError:
            raise self._unreachable() from None
        _note_usage(self.backend_id, response.headers)
        return self._interpret(response, key)

    async def open_stream(self, url: str, payload: dict[str, Any]) -> ChatUpstream:
        key = self.require_key()
        client = self.client()
        try:
            opener = client.stream("POST", url, headers=self._headers(key), json=payload)
            response = await opener.__aenter__()
        except httpx.TimeoutException:
            await client.aclose()
            raise self._timeout() from None
        except ssl.SSLError:
            await client.aclose()
            raise self._unreachable(ssl_error=True) from None
        except httpx.HTTPError:
            await client.aclose()
            raise self._unreachable() from None
        except BaseException:
            await client.aclose()
            raise

        async def _close() -> None:
            await opener.__aexit__(None, None, None)
            await client.aclose()

        _note_usage(self.backend_id, response.headers)

        if response.is_redirect or response.status_code >= 400:
            raw = await response.aread()
            await _close()
            raise self._error_result(
                response.status_code,
                raw,
                key,
                redirect=response.is_redirect,
                headers=response.headers,
            )

        media = response.headers.get("content-type", "")
        if "text/event-stream" not in media.lower():
            raw = await response.aread()
            await _close()
            if response.status_code < 400 and _is_json(raw):
                return ChatUpstream(
                    status_code=response.status_code,
                    media_type="application/json",
                    body=redact_bytes(raw, [key]),
                )
            raise GatewayError(
                502,
                f"{self.label} stream was not text/event-stream",
                "upstream_error",
                f"{self.backend_id}_bad_stream",
            )

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield redact_bytes(chunk, [key])
            except ssl.SSLError:
                logger.warning("upstream SSL error mid-stream backend=%s", self.backend_id)
                yield sse_error(ssl_failure())
            finally:
                await _close()

        return ChatUpstream(
            status_code=response.status_code,
            media_type="text/event-stream",
            stream=chunks(),
        )

    def _interpret(self, response: httpx.Response, key: str) -> ChatUpstream:
        if response.is_redirect:
            raise self._error_result(
                response.status_code, response.content, key, redirect=True, headers=response.headers
            )
        if response.status_code >= 400:
            raise self._error_result(
                response.status_code,
                response.content,
                key,
                redirect=False,
                headers=response.headers,
            )
        body = redact_bytes(response.content, [key])
        if not _is_json(body):
            raise GatewayError(
                502,
                f"{self.label} returned a non-JSON body",
                "upstream_error",
                f"{self.backend_id}_bad_body",
            )
        return ChatUpstream(
            status_code=response.status_code,
            media_type="application/json",
            body=body,
        )

    def _timeout(self) -> GatewayError:
        return GatewayError(
            504,
            f"{self.label} request timed out",
            "upstream_error",
            f"{self.backend_id}_timeout",
        )

    def _unreachable(self, *, ssl_error: bool = False) -> GatewayError:
        if ssl_error:
            return ssl_failure()
        return GatewayError(
            502,
            f"{self.label} unreachable",
            "upstream_error",
            f"{self.backend_id}_unreachable",
        )

    def _error_result(
        self,
        status_code: int,
        raw: bytes,
        key: str,
        *,
        redirect: bool,
        headers: Mapping[str, str] | None = None,
    ) -> GatewayError:
        if redirect or status_code in {301, 302, 303, 307, 308}:
            return GatewayError(
                502,
                f"{self.label} redirect refused",
                "upstream_error",
                f"{self.backend_id}_redirect",
                extra=self._extra(status_code, ""),
            )
        text = redact(raw.decode("utf-8", errors="replace"), [key])
        return GatewayError(
            502,
            f"{self.label} upstream error",
            "upstream_error",
            f"{self.backend_id}_upstream_error",
            extra=self._extra(status_code, text[:4000]),
            retry_after=parse_retry_after((headers or {}).get("retry-after")),
        )

    def _extra(self, status_code: int, body: str) -> dict[str, Any]:
        detail: dict[str, Any] = {"upstream_status": status_code}
        if body:
            detail["upstream_body"] = body
        return {
            self.backend_id: detail,
            "upstream": {"backend": self.backend_id, **detail},
        }


def _is_json(body: bytes) -> bool:
    import json

    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def describe_upstream_failure(
    exc: GatewayError, *, backend_id: str, label: str, model: str
) -> GatewayError:
    """Make an upstream failure say what the bridge sent and what to fix.

    A bare "upstream error" is unusable to a first-time configurer. A 401 or
    403 is the credential and a 429 is a rate limit, so neither may send them
    hunting for a model id; any other status keeps the model-id advice, because
    that is the usual cause.
    The upstream body is preserved either way.
    """
    if exc.code not in {f"{backend_id}_upstream_error", f"{backend_id}_unavailable"}:
        return exc
    status = (exc.extra.get("upstream") or {}).get("upstream_status")
    extra = dict(exc.extra)
    extra["model"] = model
    if status in {401, 403}:
        message = f"{label} rejected the API key (upstream HTTP {status})"
        extra["hint"] = (
            f"{label} answered HTTP {status} to the key this bridge sent. The key is "
            "wrong, expired, or lacks access. Replace it with ./nexus setup "
            f"--backend {backend_id} or on the dashboard at /, then retry."
        )
    elif status == 429:
        message = f"{label} is rate limiting this key (upstream HTTP 429)"
        extra["hint"] = (
            f"{label} answered HTTP 429: too many requests or a free-tier limit, not a bad "
            "model id. Wait and try again"
            + (f" (it asked for {int(exc.retry_after)}s)" if exc.retry_after is not None else "")
            + ", or switch to a model or key with more quota."
        )
    else:
        message = f"{label} rejected model '{model}' (upstream HTTP {status})"
        extra["hint"] = (
            f"The bridge sent model '{model}' and {label} answered HTTP {status}. "
            "Check the model id: set NEXUS_MODEL to a model your account can "
            f"reach, or see what {label} offers with GET /v1/models."
        )
    return GatewayError(
        exc.status_code,
        message,
        exc.err_type,
        exc.code,
        extra=extra,
        retry_after=exc.retry_after,
    )


def _note_usage(backend_id: str, headers: Mapping[str, str]) -> None:
    """Keep the provider's own rate-limit numbers for /dashboard/usage. Never raises."""
    try:
        from nexus.usage import store

        store().record_headers(backend_id, headers)
    except Exception:
        pass
