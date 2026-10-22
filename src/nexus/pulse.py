"""In-memory ring of recent /v1 calls. Nothing here is written to disk.

Entries keep a route, a model id, a status, a latency, and optional token
counts when the upstream reported them. They do not keep headers, bodies, or
keys. A model that looks like credential material is dropped.

Lifetime counters cover this process only. They are not a Claude / ChatGPT /
Grok plan balance.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nexus.config import valid_model_id
from nexus.redact import known_secrets, redact

logger = logging.getLogger("nexus")

PULSE_CAP = 40
QUOTA_COOLDOWN_S = 900.0
_CODE = re.compile(r"^[a-z0-9_-]{1,64}$")
_HINT_MAX = 400
_model: ContextVar[str | None] = ContextVar("os3_pulse_model", default=None)
_prompt_tokens: ContextVar[int | None] = ContextVar("os3_pulse_prompt_tokens", default=None)
_completion_tokens: ContextVar[int | None] = ContextVar("os3_pulse_completion_tokens", default=None)
_token_box: ContextVar[list[int | None] | None] = ContextVar("os3_pulse_token_box", default=None)
_usage_limit: ContextVar[bool] = ContextVar("os3_pulse_usage_limit", default=False)
_backend: ContextVar[str | None] = ContextVar("os3_pulse_backend", default=None)
_error_code: ContextVar[str | None] = ContextVar("os3_pulse_error_code", default=None)
_error_hint: ContextVar[str | None] = ContextVar("os3_pulse_error_hint", default=None)
_KEY_PREFIXES = ("sk-", "sk_", "os3_", "xai-", "xai_", "bearer")
_tool_repair_lock = threading.Lock()
_tool_repair_counts = {"repaired": 0, "rejected": 0}


def note_model(model: object) -> None:
    """Remember the model for the request in flight. Sanitized when recorded."""
    if isinstance(model, str) and model.strip():
        _model.set(model.strip())
    else:
        _model.set(None)


def note_tokens(*, prompt: object = None, completion: object = None) -> None:
    """Remember token counts the upstream reported for this request, if any."""
    counted = (_token_count(prompt), _token_count(completion))
    _prompt_tokens.set(counted[0])
    _completion_tokens.set(counted[1])
    box = _token_box.get()
    if box is not None:
        box[:] = counted


def note_usage_limit(hit: bool = True) -> None:
    """Mark that this request hit an upstream usage or subscription rate limit."""
    _usage_limit.set(bool(hit))


def note_backend(backend_id: object) -> None:
    """Remember which backend the request in flight was handed to."""
    _backend.set(backend_id.strip() if isinstance(backend_id, str) and backend_id.strip() else None)


def note_error(code: object, hint: object) -> None:
    """Remember the error code and hint the client was sent for this request.

    Both are the bridge's own strings (a ``GatewayError`` code and its fix), never an
    upstream body. Anything that does not look like that is dropped.
    """
    _error_code.set(code if isinstance(code, str) and _CODE.match(code) else None)
    text = hint.strip() if isinstance(hint, str) else ""
    if text:
        text = redact(text, known_secrets())[:_HINT_MAX]
    _error_hint.set(text or None)


def note_tool_repair(outcome: str) -> None:
    """Count one tool-call answer the bridge repaired or rejected. Process-wide, no content."""
    if outcome in _tool_repair_counts:
        with _tool_repair_lock:
            _tool_repair_counts[outcome] += 1


def _token_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value == int(value):
        return int(value)
    return None


def safe_model(value: object) -> str | None:
    """A model id safe to show on the dashboard, or None."""
    if not isinstance(value, str):
        return None
    model = value.strip()
    if not valid_model_id(model):
        return None
    if model.lower().startswith(_KEY_PREFIXES):
        return None
    if redact(model, known_secrets()) != model:
        return None
    return model


def safe_route(method: object, path: object) -> str:
    """`METHOD /v1/...` with the query stripped and any secret path dropped."""
    verb = str(method or "GET").upper()
    if not verb.isalpha() or len(verb) > 10:
        verb = "GET"
    raw = str(path or "/")
    raw = raw.split("?", 1)[0]
    if not raw.startswith("/v1"):
        raw = "/v1"
    if len(raw) > 96:
        raw = raw[:96]
    text = f"{verb} {raw}"
    if redact(text, known_secrets()) != text:
        return f"{verb} /v1"
    return text


@dataclass(frozen=True, slots=True)
class PulseEntry:
    ts: str
    route: str
    model: str | None
    status: int
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_limit: bool = False
    backend: str | None = None
    error_code: str | None = None
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ts": self.ts,
            "route": self.route,
            "model": self.model,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "usage_limit": self.usage_limit,
            "backend": self.backend,
            "error_code": self.error_code,
            "hint": self.hint,
        }
        return body


def _percentile(sorted_vals: list[int], pct: float) -> int | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (len(sorted_vals) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(sorted_vals) - 1)
    weight = rank - low
    return int(round(sorted_vals[low] * (1 - weight) + sorted_vals[high] * weight))


class PulseLog:
    """Newest-last deque, exposed newest-first. Capped.

    Lifetime counters cover this process since the log was created. They reset
    on restart. They are not a provider plan balance.
    """

    def __init__(self, cap: int = PULSE_CAP, *, count_usage: bool = True) -> None:
        if cap < 1:
            raise ValueError("pulse cap must be at least 1")
        self.cap = cap
        self.count_usage = count_usage
        self._items: deque[PulseEntry] = deque(maxlen=cap)
        self._lock = threading.Lock()
        self._started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._calls = 0
        self._ok = 0
        self._client_error = 0
        self._server_error = 0
        self._usage_limit = 0
        self._images = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._token_calls = 0
        self._model_remapped = 0
        self._last_post: dict[str, tuple[float, bool]] = {}

    def record(
        self,
        *,
        method: str,
        path: str,
        model: object,
        status: int,
        latency_ms: int,
        prompt_tokens: object = None,
        completion_tokens: object = None,
        usage_limit: bool = False,
        backend: object = None,
        error_code: object = None,
        hint: object = None,
    ) -> None:
        code = status if isinstance(status, int) else 500
        prompt = _token_count(prompt_tokens)
        completion = _token_count(completion_tokens)
        hit_limit = bool(usage_limit) or code == 429
        entry = PulseEntry(
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            route=safe_route(method, path),
            model=safe_model(model),
            status=code,
            latency_ms=max(0, latency_ms if isinstance(latency_ms, int) else 0),
            prompt_tokens=prompt,
            completion_tokens=completion,
            usage_limit=hit_limit,
            backend=backend if isinstance(backend, str) and backend else None,
            error_code=error_code if isinstance(error_code, str) else None,
            hint=hint if isinstance(hint, str) else None,
        )
        counted: str | None = None
        with self._lock:
            self._items.append(entry)
            if entry.backend is not None and entry.route.startswith("POST "):
                self._last_post[entry.backend] = (time.monotonic(), hit_limit)
                counted = entry.backend
            self._calls += 1
            if entry.route == "POST /v1/images/generations" and 200 <= code < 300:
                self._images += 1
            if 200 <= code < 400:
                self._ok += 1
            elif 400 <= code < 500:
                self._client_error += 1
            else:
                self._server_error += 1
            if hit_limit:
                self._usage_limit += 1
            if prompt is not None or completion is not None:
                self._token_calls += 1
                self._prompt_tokens += prompt or 0
                self._completion_tokens += completion or 0
        if counted is not None and self.count_usage:
            try:
                from nexus.usage import store

                tokens = None
                if prompt is not None or completion is not None:
                    tokens = (prompt or 0) + (completion or 0)
                store().record_call(counted, tokens)
            except Exception:
                logger.debug("usage count failed")

    def note_model_remapped(self) -> None:
        """Count one request served on the active model instead of the id it named."""
        with self._lock:
            self._model_remapped += 1

    def quota_hit(self, backend: str, *, cooldown: float = QUOTA_COOLDOWN_S) -> bool:
        """True when the latest chat call through ``backend`` hit a usage limit, recently.

        Only POST calls count, so a successful GET /v1/models does not clear it. This is
        process-local traffic, not a plan balance, and a plain 429 looks the same.
        """
        with self._lock:
            last = self._last_post.get(backend)
        if last is None or not last[1]:
            return False
        return time.monotonic() - last[0] < cooldown

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        return [item.as_dict() for item in reversed(items)]

    def traffic(self) -> dict[str, Any]:
        """Process-local traffic. Not a subscription plan balance."""
        with self._lock:
            latencies = sorted(item.latency_ms for item in self._items)
            payload = {
                "since": self._started,
                "calls": self._calls,
                "ok": self._ok,
                "client_error": self._client_error,
                "server_error": self._server_error,
                "usage_limit": self._usage_limit,
                "images": self._images,
                "latency_ms": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "max": latencies[-1] if latencies else None,
                    "sample": len(latencies),
                },
                "tokens": {
                    "prompt": self._prompt_tokens if self._token_calls else None,
                    "completion": self._completion_tokens if self._token_calls else None,
                    "total": (
                        self._prompt_tokens + self._completion_tokens if self._token_calls else None
                    ),
                    "known_calls": self._token_calls,
                    "unknown_calls": max(0, self._calls - self._token_calls),
                },
                "tool_repair": tool_repair_counts(),
                "model_remapped": self._model_remapped,
                "note": (
                    "Traffic through this door since the process started. "
                    "Not your Claude, ChatGPT, or Grok plan balance."
                ),
            }
        return payload


def tool_repair_counts() -> dict[str, int]:
    with _tool_repair_lock:
        return dict(_tool_repair_counts)


def _is_v1(path: object) -> bool:
    return path == "/v1" or (isinstance(path, str) and path.startswith("/v1/"))


class PulseMiddleware:
    """Time /v1 requests. Other routes are not recorded."""

    def __init__(self, app: Callable[..., Awaitable[None]], pulse: PulseLog) -> None:
        self.app = app
        self.pulse = pulse

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or not _is_v1(scope.get("path")):
            await self.app(scope, receive, send)
            return
        model_token = _model.set(None)
        prompt_token = _prompt_tokens.set(None)
        completion_token = _completion_tokens.set(None)
        box: list[int | None] = [None, None]
        box_token = _token_box.set(box)
        limit_token = _usage_limit.set(False)
        backend_token = _backend.set(None)
        code_token = _error_code.set(None)
        hint_token = _error_hint.set(None)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                code = message.get("status", 500)
                if isinstance(code, int):
                    status = code
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                self.pulse.record(
                    method=str(scope.get("method") or "GET"),
                    path=str(scope.get("path") or "/v1"),
                    model=_model.get(),
                    status=status,
                    latency_ms=elapsed_ms,
                    prompt_tokens=box[0] if box[0] is not None else _prompt_tokens.get(),
                    completion_tokens=box[1] if box[1] is not None else _completion_tokens.get(),
                    usage_limit=_usage_limit.get(),
                    backend=_backend.get(),
                    error_code=_error_code.get(),
                    hint=_error_hint.get(),
                )
            except Exception as exc:
                logger.error("pulse record failed: %s", type(exc).__name__)
            finally:
                _model.reset(model_token)
                _prompt_tokens.reset(prompt_token)
                _completion_tokens.reset(completion_token)
                _token_box.reset(box_token)
                _usage_limit.reset(limit_token)
                _backend.reset(backend_token)
                _error_code.reset(code_token)
                _error_hint.reset(hint_token)
