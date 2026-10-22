"""Cross-provider failover when the active backend is out of quota.

A chain such as ``codex, claude-code, nous, openrouter`` is configured; the active
backend is always tried first. A request that fails with ``out_of_quota`` (see
``errors.classify_failure``, and nothing else) is retried on the next backend that
is present and not cooling down, before a byte has gone to the client. The exhausted
backend then sits out ``cooldown_s``, after which it is tried first again.

Off unless a chain names a second backend, so a single-backend setup runs exactly the
code it ran before. State is in memory and per process. Nothing here logs a prompt,
a response, or a credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from nexus import usage
from nexus.backend_state import READY, base_state
from nexus.backends.base import Backend, ChatUpstream
from nexus.config import (
    BACKEND_IDS,
    CLI_BIN_ENV,
    backend_model,
    canonical_backend,
    environ_get,
    fallback_file_path,
)
from nexus.errors import (
    OUT_OF_QUOTA,
    RATE_LIMITED,
    UNSUPPORTED,
    GatewayError,
    classify_failure,
)
from nexus.pulse import note_backend, note_model

logger = logging.getLogger("nexus")

ENV_CHAIN = "NEXUS_FALLBACK"
ENV_COOLDOWN = "NEXUS_FALLBACK_COOLDOWN"
DEFAULT_COOLDOWN_S = 900.0
USAGE_THRESHOLD = "usage_threshold"
DEFAULT_RATE_COOLDOWN_S = 60.0
_NO_IMAGES = frozenset({"grok-build", "antigravity"})
_IMAGE_PARTS = frozenset({"image_url", "input_image", "image"})
_MAX_COOLDOWN_S = 86400.0
_OFF = {"off", "none", "0", "false", "no"}


@dataclass(frozen=True)
class FallbackConfig:
    chain: tuple[str, ...]
    cooldown_s: float
    source: Literal["env", "file", "none"]
    problems: tuple[str, ...] = ()


def _names(raw: object, where: str, problems: list[str]) -> list[str]:
    if isinstance(raw, str):
        items: list[object] = [p for p in re.split(r"[,\s]+", raw) if p]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        problems.append(f"{where} must be a list of backend names")
        return []
    out: list[str] = []
    for position, item in enumerate(items, start=1):
        name = canonical_backend(item) if isinstance(item, str) else ""
        if name in BACKEND_IDS:
            if name not in out:
                out.append(name)
            continue
        problems.append(
            f"{where} entry {position} is not a backend name; use: {', '.join(sorted(BACKEND_IDS))}"
        )
    return out


def _read_file() -> tuple[dict[str, Any], list[str]]:
    path = fallback_file_path()
    try:
        if not path.is_file():
            return {}, []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, [f"{path.name} could not be read as JSON; fix or delete it"]
    if not isinstance(data, dict):
        return {}, [f'{path.name} must be a JSON object like {{"chain": ["codex", "nous"]}}']
    return data, []


def fallback_config() -> FallbackConfig:
    """Env first, then the JSON file. Read on each call so edits apply live."""
    problems: list[str] = []
    data, file_problems = _read_file()
    problems += file_problems
    chain: list[str] = []
    source: Literal["env", "file", "none"] = "none"
    env = (environ_get(ENV_CHAIN) or "").strip()
    if env:
        source = "env"
        if env.lower() not in _OFF:
            chain = _names(env, ENV_CHAIN, problems)
    elif "chain" in data:
        source = "file"
        chain = _names(data["chain"], f'"chain" in {fallback_file_path().name}', problems)
    cooldown = DEFAULT_COOLDOWN_S
    raw = (environ_get(ENV_COOLDOWN) or "").strip() or data.get("cooldown_s")
    if raw not in (None, ""):
        try:
            cooldown = float(raw)
            if not cooldown >= 1:
                raise ValueError
        except (TypeError, ValueError):
            cooldown = DEFAULT_COOLDOWN_S
            problems.append(f"{ENV_COOLDOWN} / cooldown_s must be a number of seconds, 1 or more")
    return FallbackConfig(tuple(chain), min(cooldown, _MAX_COOLDOWN_S), source, tuple(problems))


def effective_chain(config: FallbackConfig, primary: str) -> list[str]:
    """The active backend first, then the configured chain in order, without repeats."""
    return [primary, *(name for name in config.chain if name != primary)]


def chain_warnings(chain: list[str]) -> list[str]:
    """Plain-language cautions about where a failover would send the conversation."""
    if len(chain) < 2:
        return []
    cli = [n for n in chain if n in CLI_BIN_ENV]
    billed = [n for n in chain if n not in CLI_BIN_ENV]
    if cli and billed and chain.index(cli[0]) < chain.index(billed[-1]):
        return [
            f"The chain moves from a subscription backend ({cli[0]}) to a key-billed one "
            f"({', '.join(n for n in billed if chain.index(n) > chain.index(cli[0]))}). "
            "At a usage limit, requests would be billed per use and the conversation "
            "would be sent to that provider."
        ]
    return []


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FailoverState:
    """Which backends are cooling down, and how often a request moved on.

    ``clock`` is monotonic seconds and is injectable so tests never sleep; ``wall`` only
    turns a deadline into a timestamp for display.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._until: dict[str, tuple[float, str]] = {}
        self.failovers = 0
        self.last: dict[str, Any] | None = None

    def _remaining(self, name: str) -> float | None:
        entry = self._until.get(name)
        if entry is None:
            return None
        left = entry[0] - self._clock()
        if left <= 0:
            del self._until[name]
            return None
        return left

    def cooling(self, name: str) -> bool:
        with self._lock:
            return self._remaining(name) is not None

    def cooling_names(self, reason: str | None = None) -> set[str]:
        with self._lock:
            return {
                n
                for n in list(self._until)
                if self._remaining(n) is not None and reason in (None, self._until[n][1])
            }

    def start_cooldown(self, name: str, seconds: float, reason: str = OUT_OF_QUOTA) -> str:
        """Put ``name`` out for ``seconds``. Returns when it is due back, as a timestamp."""
        with self._lock:
            self._until[name] = (self._clock() + seconds, reason)
        return _iso(self._wall() + seconds)

    def record_failover(
        self, source: str, target: str, until: str, reason: str = OUT_OF_QUOTA
    ) -> None:
        with self._lock:
            self.failovers += 1
            self.last = {
                "from": source,
                "to": target,
                "reason": reason,
                "cooldown_until": until,
                "at": _iso(self._wall()),
            }

    def snapshot(self, config: FallbackConfig, primary: str) -> dict[str, Any]:
        """The ``fallback`` object of /health. Additive; names and times only."""
        chain = effective_chain(config, primary)
        with self._lock:
            cooling = []
            for name in chain:
                left = self._remaining(name)
                if left is not None:
                    cooling.append(
                        {
                            "backend": name,
                            "reason": self._until[name][1],
                            "until": _iso(self._wall() + left),
                            "remaining_s": int(left),
                        }
                    )
            hold = {c["backend"] for c in cooling}
            return {
                "enabled": len(chain) > 1,
                "chain": chain if len(chain) > 1 else [],
                "primary": primary,
                "active": next((n for n in chain if n not in hold), primary),
                "cooldown_s": config.cooldown_s,
                "cooling": cooling,
                "failovers": self.failovers,
                "last_failover": dict(self.last) if self.last else None,
                "source": config.source,
                "problems": list(config.problems),
                "warnings": chain_warnings(chain),
            }


class FailoverBackend:
    """A ``Backend`` whose ``open_chat`` walks the chain when a backend has run out.

    Only ``open_chat`` is wrapped, so everything downstream of it (the tool-call
    repair, the SSE terminator) sees one ordinary backend. ``open_chat`` returns
    after the upstream accepted the request and, for a stream, produced its first
    bytes; nothing has reached the client by then, which is what makes a retry safe.
    A stream that breaks later is not retried.

    A backend that hit its quota sits out ``cooldown_s``; one that was only rate limited,
    the upstream's Retry-After (60 s without one). A fallback that cannot serve the
    request at all (no tools, no images, no such model) is skipped with no cooldown.
    A failover is counted and logged only once a backend has served the request.
    """

    def __init__(
        self,
        primary: Backend,
        chain: list[str],
        state: FailoverState,
        cooldown_s: float,
        resolve: Callable[[str], Backend],
    ) -> None:
        self._primary = primary
        self._chain = chain
        self._state = state
        self._cooldown_s = cooldown_s
        self._resolve = resolve
        self.id = primary.id
        self.served_by: str | None = None

    def describe(self) -> dict[str, Any]:
        return self._primary.describe()

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._primary.list_models()

    def _pause(self, exc: GatewayError, kind: str) -> float:
        if kind == OUT_OF_QUOTA:
            return self._cooldown_s
        wait = exc.retry_after if exc.retry_after is not None else DEFAULT_RATE_COOLDOWN_S
        return min(max(wait, 1.0), self._cooldown_s)

    def _bench_if_nearly_spent(self, primary_id: str) -> None:
        """Cool the active backend early when its own reported usage is at the threshold.

        Only real, provider-reported numbers count (``nexus.usage``), never the local
        estimate. The cooldown lasts until that window resets, so the next backend in
        the chain serves until then, through the same machinery as a usage limit.
        """
        if len(self._chain) < 2 or self._state.cooling(primary_id):
            return
        threshold = usage.switch_threshold()
        if threshold is None:
            return
        hit = usage.store().over_threshold(primary_id, threshold)
        if hit is None:
            return
        percent, reset_in = hit
        until = self._state.start_cooldown(
            primary_id, min(max(60.0, reset_in), _MAX_COOLDOWN_S), USAGE_THRESHOLD
        )
        logger.warning(
            "usage threshold %s at %.0f%% (>= %.0f%%): benched until %s",
            primary_id,
            percent,
            threshold,
            until,
        )

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        primary_id = self._primary.id
        self._bench_if_nearly_spent(primary_id)
        tools = bool(payload.get("tools"))
        images = _has_image(payload.get("messages"))
        order = [
            n
            for n in self._chain
            if not self._state.cooling(n)
            and (n == primary_id or _can_serve(n, tools=tools, images=images))
        ]
        failures: list[tuple[str, str, str]] = []
        skipped: list[str] = []
        last: GatewayError | None = None
        for name in order:
            first = name == primary_id
            backend = self._primary if first else self._resolve(name)
            if not first and not await _present(backend):
                continue
            body = payload if first else {**payload, "model": backend_model(name)}
            if images and not first and not await _takes_images(backend, body["model"]):
                logger.info("fallback skip %s: model does not take images", name)
                skipped.append(name)
                continue
            try:
                upstream = await backend.open_chat(body, stream=stream)
            except GatewayError as exc:
                kind = classify_failure(exc)
                if kind in (OUT_OF_QUOTA, RATE_LIMITED):
                    last = exc
                    until = self._state.start_cooldown(name, self._pause(exc, kind), kind)
                    failures.append((name, until, kind))
                    continue
                if kind == UNSUPPORTED and not first:
                    skipped.append(name)
                    continue
                raise
            self._served(name, backend, body, failures, first)
            return upstream
        if not failures:
            return await self._primary.open_chat(payload, stream=stream)
        assert last is not None
        if len(failures) == 1:
            if skipped:
                extra = dict(last.extra or {})
                extra["fallback"] = {"tried": [failures[0][0]], "skipped": skipped}
                extra["hint"] = (
                    f"{extra.get('hint', '').rstrip()} Fallback skipped "
                    f"{', '.join(skipped)}: its model does not take images (or cannot be "
                    "served this request)."
                ).strip()
                last.extra = extra
            raise last
        tried = [name for name, _, _ in failures]
        note = f" Skipped (cannot serve this request): {', '.join(skipped)}." if skipped else ""
        raise GatewayError(
            429,
            "Every backend in the fallback chain is out of quota or rate limited",
            "rate_limit_error",
            "fallback_exhausted",
            extra={
                "hint": (
                    f"Tried {', '.join(tried)}; each hit a limit and is cooling down.{note} "
                    "Wait for a window to reset, add a backend to NEXUS_FALLBACK, "
                    "or GET /health to see when each is due back."
                ),
                "fallback": {"tried": tried, "skipped": skipped},
            },
        )

    def _served(
        self,
        name: str,
        backend: Backend,
        body: dict[str, Any],
        failures: list[tuple[str, str, str]],
        first: bool,
    ) -> None:
        self.id = backend.id
        self._chain = self._chain[self._chain.index(name) :]
        note_backend(backend.id)
        if not first:
            self.served_by = backend.id
            note_model(body.get("model"))
        for i, (failed, until, kind) in enumerate(failures):
            target = failures[i + 1][0] if i + 1 < len(failures) else name
            self._state.record_failover(failed, target, until, kind)
            logger.warning(
                "failover %s -> %s reason=%s cooldown_until=%s", failed, target, kind, until
            )


def _has_image(messages: object) -> bool:
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in _IMAGE_PARTS for part in content
        ):
            return True
    return False


async def _takes_images(backend: Backend, model: object) -> bool:
    """False unless a fallback backend can say its model takes image input."""
    check = getattr(backend, "accepts_images", None)
    if check is None:
        return True
    try:
        return bool(await check(str(model))) if isinstance(model, str) else False
    except Exception:
        return False


def _can_serve(name: str, *, tools: bool, images: bool) -> bool:
    """False for a backend that is known to refuse what this request carries."""
    if tools and name in CLI_BIN_ENV:
        return False
    return not (images and name in _NO_IMAGES)


async def _present(backend: Backend) -> bool:
    """Usable by the row /health shows: CLI present and signed in, or a key set."""
    try:
        row = await asyncio.to_thread(backend.describe)
    except Exception:
        return False
    return base_state(row) == READY


def with_fallback(
    primary: Backend, state: FailoverState, resolve: Callable[[str], Backend]
) -> Backend:
    """``primary`` itself unless a chain names another backend (the default is none)."""
    config = fallback_config()
    chain = effective_chain(config, primary.id)
    if len(chain) < 2:
        return primary
    return FailoverBackend(primary, chain, state, config.cooldown_s, resolve)
