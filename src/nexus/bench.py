"""POST /dashboard/bench: time one prompt across chosen backend/model pairs.

Each target is called in this process through the same backend objects chat uses,
one after another so they do not compete. It does not go through /v1, so it needs no
bridge key and is not in the pulse log. The response carries timings, token counts
the upstream reported, and an error code and hint per row. It never carries the
prompt, the completion text, or a key.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nexus.backend_state import kind_of
from nexus.backends.base import Backend
from nexus.backends.registry import build_backend
from nexus.config import (
    BACKEND_IDS,
    canonical_backend,
    model_for,
    upstream_timeout,
    valid_model_id,
)
from nexus.errors import DashboardError, GatewayError
from nexus.pulse import _token_count
from nexus.redact import known_secrets, redact

MAX_TARGETS = 6
MAX_PROMPT_CHARS = 2000
DEFAULT_MAX_TOKENS = 64
MAX_MAX_TOKENS = 256
MAX_TIMEOUT_S = 60.0
_HINT_MAX = 400


@dataclass(frozen=True)
class Target:
    backend: str
    model: str


@dataclass(frozen=True)
class BenchRequest:
    targets: list[Target]
    prompt: str
    max_tokens: int
    timeout_s: float


def _invalid(message: str) -> DashboardError:
    return DashboardError(400, message, "invalid_request")


def _limits(message: str) -> DashboardError:
    return DashboardError(400, message, "bench_limits")


def parse_request(payload: object, *, active: str) -> BenchRequest:
    """Validate a bench body. ``active`` is the backend a target without one uses."""
    if not isinstance(payload, dict):
        raise _invalid("Expected a JSON object.")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise _invalid("targets must be a non-empty list of {backend, model} objects.")
    if len(raw_targets) > MAX_TARGETS:
        raise _limits(f"A bench takes at most {MAX_TARGETS} targets.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _invalid("prompt must be non-empty text.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise _limits(f"prompt is at most {MAX_PROMPT_CHARS} characters.")
    max_tokens = payload.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        raise _invalid("max_tokens must be an integer.")
    if not 1 <= max_tokens <= MAX_MAX_TOKENS:
        raise _limits(f"max_tokens is 1 to {MAX_MAX_TOKENS}.")
    timeout = payload.get("timeout_s", min(MAX_TIMEOUT_S, upstream_timeout()))
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise _invalid("timeout_s must be a number.")
    if not 1 <= timeout <= MAX_TIMEOUT_S:
        raise _limits(f"timeout_s is 1 to {int(MAX_TIMEOUT_S)}.")
    targets: list[Target] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            raise _invalid("Each target must be an object.")
        backend = item.get("backend")
        if backend is None:
            chosen = active
        elif isinstance(backend, str) and backend.strip():
            chosen = canonical_backend(backend)
        else:
            raise _invalid("target backend must be text.")
        if chosen not in BACKEND_IDS:
            raise DashboardError(400, f"Unknown backend '{chosen}'.", "unknown_backend")
        model = item.get("model")
        if model is None:
            model = model_for(chosen)
        if not isinstance(model, str) or not valid_model_id(model.strip()):
            raise _invalid("target model must be one id of letters, numbers, and . _ : / -")
        targets.append(Target(chosen, model.strip()))
    return BenchRequest(targets, prompt, max_tokens, float(timeout))


def _text(body: bytes) -> tuple[str, dict[str, Any]]:
    """Assistant text and usage from a chat completion body; empty when it has neither."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", {}
    if not isinstance(payload, dict):
        return "", {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content, usage
    return "", usage


def _positive(value: object) -> int | None:
    count = _token_count(value)
    return count if count else None


def _error(code: str, hint: str) -> dict[str, str]:
    return {"code": code, "hint": redact(hint, known_secrets())[:_HINT_MAX]}


def _row(target: Target, kind: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "backend": target.backend,
        "kind": kind,
        "model": target.model,
        "ok": False,
        "total_ms": 0,
        "input_tokens": None,
        "output_tokens": None,
        "tok_per_s": None,
        "error": None,
    }
    row.update(fields)
    return row


async def _run_one(
    target: Target,
    request: BenchRequest,
    *,
    backend: Backend,
    gate: asyncio.Semaphore,
) -> dict[str, Any]:
    describe = await asyncio.to_thread(backend.describe)
    kind = kind_of(describe)
    if describe.get("ready") is not True:
        return _row(
            target,
            kind,
            error=_error("backend_not_ready", str(describe.get("hint") or "backend is not ready")),
        )
    payload = {
        "model": target.model,
        "messages": [{"role": "user", "content": request.prompt}],
        "stream": False,
        "max_tokens": request.max_tokens,
    }
    started = time.perf_counter()
    try:
        async with gate:
            upstream = await asyncio.wait_for(
                backend.open_chat(payload, stream=False), timeout=request.timeout_s
            )
    except TimeoutError:
        return _row(
            target,
            kind,
            total_ms=int((time.perf_counter() - started) * 1000),
            error=_error(
                "bench_timeout",
                f"No answer within {int(request.timeout_s)} s. "
                "Raise timeout_s or try a faster model.",
            ),
        )
    except GatewayError as exc:
        return _row(
            target,
            kind,
            total_ms=int((time.perf_counter() - started) * 1000),
            error=_error(exc.code, exc.as_dict()["error"]["hint"]),
        )
    except Exception:
        return _row(
            target,
            kind,
            total_ms=int((time.perf_counter() - started) * 1000),
            error=_error("internal_error", "The bridge failed on this target. GET /doctor."),
        )
    total_ms = int((time.perf_counter() - started) * 1000)
    text, usage = _text(upstream.body or b"")
    if upstream.status_code != 200 or not text.strip():
        return _row(
            target,
            kind,
            total_ms=total_ms,
            error=_error("bench_empty", "The backend answered without any assistant text."),
        )
    output = _positive(usage.get("completion_tokens", usage.get("output_tokens")))
    prompt = _positive(usage.get("prompt_tokens", usage.get("input_tokens")))
    rate = None
    if kind == "rest" and output is not None and total_ms > 0:
        rate = round(output / (total_ms / 1000), 1)
    return _row(
        target,
        kind,
        ok=True,
        total_ms=total_ms,
        input_tokens=prompt,
        output_tokens=output,
        tok_per_s=rate,
    )


async def run_bench(
    request: BenchRequest,
    *,
    gate: asyncio.Semaphore,
    override: Backend | None = None,
    cancelled: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[dict[str, Any]] = []
    for target in request.targets:
        if cancelled is not None and await cancelled():
            break
        if override is not None and target.backend == override.id:
            backend = override
        else:
            backend = build_backend(target.backend)
        rows.append(await _run_one(target, request, backend=backend, gate=gate))
    return {"ok": True, "started_at": started_at, "rows": rows}
