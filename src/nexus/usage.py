"""Plan usage and reset times per backend, persisted to a small local JSON file.

Two kinds of numbers, never mixed:

- **real**: what the provider reported on its own responses. The ChatGPT Codex
  endpoint sends ``x-codex-primary-*`` / ``x-codex-secondary-*`` (used percent, window
  length, reset time). OpenAI-style APIs (Nous, OpenAI, xAI, OpenRouter) send
  ``x-ratelimit-limit-*`` / ``-remaining-*`` / ``-reset-*``. OpenRouter also answers
  ``GET /api/v1/key`` with its credit usage and limit (read by the endpoint, not here).
  The ChatGPT plan also answers ``GET /backend-api/wham/usage`` (what the Codex CLI's
  ``/status`` reads) with the same windows, so the dashboard can refresh them without
  sending a chat. Every real reading carries ``from`` (``response headers`` or
  ``usage endpoint``) and ``updated_at``, so its age can be shown.
- **local counts**: requests this door sent per backend in the last hour and five
  hours, counted by Nexus. Tokens next to them are the provider's own ``usage``
  numbers from each response (streamed or not); a call whose response carried none is
  counted with no tokens and reported as such, never guessed.

Only header *values that are numbers* are kept, by name, from an allow-list. No
header is stored wholesale, so nothing token-like (``x-codex-turn-state``, auth) can
land in the file. The file is written atomically (temp file + rename, mode 600).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nexus.config import environ_get, state_path

DEFAULT_THRESHOLD = 95.0
WARN_PERCENT = 80.0
CRITICAL_PERCENT = 95.0
_LOCAL_WINDOWS = (("1h", 3600), ("5h", 5 * 3600))
_KEEP_S = 5 * 3600
_MAX_EVENTS = 5000
_IMAGE_KEEP_S = 24 * 3600
_IMAGE_WINDOWS = (("1h", 3600), ("5h", 5 * 3600), ("24h", 24 * 3600))


def usage_path() -> Path:
    return state_path("USAGE_FILE", ".nexus_usage.json", ".os3_bridge_usage.json")


def switch_threshold() -> float | None:
    """Percent at which the active backend is benched early. None when switched off."""
    raw = (environ_get("NEXUS_USAGE_SWITCH_PERCENT") or "").strip().lower()
    if raw in {"off", "0", "none", "false"}:
        return None
    try:
        value = float(raw) if raw else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD
    return value if 0 < value <= 100 else DEFAULT_THRESHOLD


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(str(value).strip())
    except ValueError:
        return None
    return out if out == out and abs(out) != float("inf") else None


def parse_codex_headers(headers: Mapping[str, str], now: float) -> dict[str, Any] | None:
    """Windows from the ChatGPT Codex ``x-codex-*`` headers, or None when absent."""
    h = {k.lower(): v for k, v in headers.items()}
    windows = []
    for name, label in (("primary", "5-hour window"), ("secondary", "weekly window")):
        used = _num(h.get(f"x-codex-{name}-used-percent"))
        if used is None:
            continue
        minutes = _num(h.get(f"x-codex-{name}-window-minutes"))
        reset_at = _num(h.get(f"x-codex-{name}-reset-at"))
        after = _num(h.get(f"x-codex-{name}-reset-after-seconds"))
        if reset_at is None and after is not None:
            reset_at = now + after
        if minutes:
            label = _window_label(minutes) + " window"
        windows.append(
            {
                "id": name,
                "label": label,
                "used_percent": max(0.0, min(100.0, used)),
                "window_minutes": int(minutes) if minutes else None,
                "reset_at": int(reset_at) if reset_at else None,
            }
        )
    if not windows:
        return None
    extra: dict[str, Any] = {}
    plan = h.get("x-codex-plan-type")
    if isinstance(plan, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", plan):
        extra["plan"] = plan
    has_credits = h.get("x-codex-credits-has-credits")
    if has_credits is not None:
        extra["credits"] = {
            "has_credits": str(has_credits).lower() == "true",
            "balance": _num(h.get("x-codex-credits-balance")),
        }
    return {"windows": windows, "from": FROM_HEADERS, **extra}


FROM_HEADERS = "response headers"
FROM_ENDPOINT = "usage endpoint"


def parse_codex_usage(body: object, now: float) -> dict[str, Any] | None:
    """Windows from the ChatGPT plan's ``GET /wham/usage`` JSON, or None.

    Only numbers, booleans and the plan name are kept; the body also names the user,
    account and email, and none of that is read.
    """
    if not isinstance(body, dict):
        return None
    limits = body.get("rate_limit")
    if not isinstance(limits, dict):
        return None
    windows = []
    for name, key, label in (
        ("primary", "primary_window", "5-hour window"),
        ("secondary", "secondary_window", "weekly window"),
    ):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = _num(window.get("used_percent"))
        if used is None:
            continue
        seconds = _num(window.get("limit_window_seconds"))
        reset_at = _num(window.get("reset_at"))
        after = _num(window.get("reset_after_seconds"))
        if reset_at is None and after is not None:
            reset_at = now + after
        minutes = seconds / 60 if seconds else None
        windows.append(
            {
                "id": name,
                "label": _window_label(minutes) + " window" if minutes else label,
                "used_percent": max(0.0, min(100.0, used)),
                "window_minutes": int(minutes) if minutes else None,
                "reset_at": int(reset_at) if reset_at else None,
            }
        )
    if not windows:
        return None
    out: dict[str, Any] = {"windows": windows, "from": FROM_ENDPOINT}
    if isinstance(limits.get("limit_reached"), bool):
        out["limit_reached"] = limits["limit_reached"]
    plan = body.get("plan_type")
    if isinstance(plan, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", plan):
        out["plan"] = plan
    credits = body.get("credits")
    if isinstance(credits, dict) and isinstance(credits.get("has_credits"), bool):
        out["credits"] = {
            "has_credits": credits["has_credits"],
            "balance": _num(credits.get("balance")),
        }
    return out


def _window_label(minutes: float) -> str:
    if minutes % 10080 == 0:
        weeks = int(minutes // 10080)
        return "weekly" if weeks == 1 else f"{weeks}-week"
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}-day"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}-hour"
    return f"{int(minutes)}-minute"


_RL = re.compile(r"^x-ratelimit-(limit|remaining|reset)-(requests|tokens)(?:-(\w+))?$")


def _seconds(value: str | None) -> float | None:
    """Reset as seconds: ``59.9``, ``1s``, ``6m0s``, ``1h2m3s``, ``250ms``."""
    if value is None:
        return None
    plain = _num(value)
    if plain is not None:
        return plain
    total, found = 0.0, False
    for amount, unit in re.findall(r"([\d.]+)(ms|h|m|s)", str(value)):
        found = True
        total += float(amount) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def parse_ratelimit_headers(headers: Mapping[str, str], now: float) -> dict[str, Any] | None:
    """Windows from OpenAI-style ``x-ratelimit-*`` headers, or None when absent."""
    groups: dict[tuple[str, str], dict[str, str]] = {}
    for key, value in headers.items():
        match = _RL.match(key.lower())
        if match:
            part, kind, suffix = match.groups()
            groups.setdefault((kind, suffix or ""), {})[part] = value
    windows = []
    for (kind, suffix), parts in sorted(groups.items(), key=lambda g: (g[0][1], g[0][0])):
        limit = _num(parts.get("limit"))
        remaining = _num(parts.get("remaining"))
        if not limit or remaining is None:
            continue
        reset = _seconds(parts.get("reset"))
        span = {"": "per minute", "1h": "per hour", "1d": "per day"}.get(suffix, suffix)
        windows.append(
            {
                "id": f"{kind}{'-' + suffix if suffix else ''}",
                "label": f"{kind} {span}",
                "used_percent": round(max(0.0, min(100.0, (limit - remaining) / limit * 100)), 2),
                "limit": int(limit),
                "remaining": int(remaining),
                "window_minutes": {"": 1, "1h": 60, "1d": 1440}.get(suffix),
                "reset_at": int(now + reset) if reset is not None else None,
            }
        )
    return {"windows": windows} if windows else None


class UsageStore:
    """Thread-safe, file-backed. One instance per process (``store()``)."""

    def __init__(self, path: Path | None = None, clock: Any = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self._path or usage_path()

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict) or not isinstance(data.get("backends"), dict):
                data = {"version": 1, "backends": {}}
            self._data = data
        return self._data

    def _save(self) -> None:
        data = self._load()
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".usage.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, separators=(",", ":"))
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError:
            pass

    def _row(self, backend: str) -> dict[str, Any]:
        rows = self._load()["backends"]
        row = rows.get(backend)
        if not isinstance(row, dict):
            row = rows[backend] = {}
        return row

    def record_headers(self, backend: str, headers: Mapping[str, str]) -> bool:
        """Keep the real windows these response headers report. True when any were found."""
        now = self._clock()
        parsed = parse_codex_headers(headers, now) or parse_ratelimit_headers(headers, now)
        if parsed is None:
            return False
        return self.record_real(backend, parsed)

    def record_real(self, backend: str, parsed: dict[str, Any]) -> bool:
        """Keep a parsed provider reading (headers or a usage endpoint) as the latest."""
        now = self._clock()
        with self._lock:
            row = self._row(backend)
            row["real"] = {**parsed, "updated_at": int(now)}
            self._save()
        return True

    def record_account(self, backend: str, account: dict[str, Any]) -> None:
        """An account-level reading (OpenRouter /key). Numbers and a note only."""
        with self._lock:
            self._row(backend)["account"] = {**account, "updated_at": int(self._clock())}
            self._save()

    def record_call(self, backend: str, tokens: int | None) -> None:
        now = int(self._clock())
        with self._lock:
            row = self._row(backend)
            events = [e for e in row.get("calls") or [] if now - e[0] < _KEEP_S]
            # [time, tokens, 1 when the provider reported them]
            events.append([now, int(tokens or 0), 0 if tokens is None else 1])
            row["calls"] = events[-_MAX_EVENTS:]
            self._save()

    def record_image(self, backend: str, count: int = 1) -> None:
        """Images generated (kept 24 h: the Nous daily cap reads it). Counts only."""
        now = int(self._clock())
        with self._lock:
            row = self._row(backend)
            events = [e for e in row.get("images") or [] if now - e[0] < _IMAGE_KEEP_S]
            events.append([now, max(1, int(count))])
            row["images"] = events[-_MAX_EVENTS:]
            self._save()

    def images_since(self, backend: str, seconds: float) -> int:
        now = self._clock()
        with self._lock:
            row = self._load()["backends"].get(backend)
            events = row.get("images") if isinstance(row, dict) else None
            return sum(int(e[1]) for e in events or [] if now - e[0] < seconds)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        with self._lock:
            rows = json.loads(json.dumps(self._load()["backends"]))
        out: dict[str, dict[str, Any]] = {}
        for name, row in rows.items():
            calls = [e for e in row.pop("calls", None) or [] if now - e[0] < _KEEP_S]
            images = [e for e in row.pop("images", None) or [] if now - e[0] < _IMAGE_KEEP_S]
            row["estimate"] = (
                {
                    f"requests_{label}": sum(1 for e in calls if now - e[0] < span)
                    for label, span in _LOCAL_WINDOWS
                }
                | {
                    f"tokens_{label}": sum(e[1] for e in calls if now - e[0] < span)
                    for label, span in _LOCAL_WINDOWS
                }
                | {
                    f"token_reports_{label}": sum(
                        1 for e in calls if now - e[0] < span and len(e) > 2 and e[2]
                    )
                    for label, span in _LOCAL_WINDOWS
                }
            )
            if images:
                row["estimate"] |= {
                    f"images_{label}": sum(int(e[1]) for e in images if now - e[0] < span)
                    for label, span in _IMAGE_WINDOWS
                }
                row["images_share_plan"] = name == "chatgpt"
            real = row.get("real")
            if isinstance(real, dict):
                updated = real.get("updated_at")
                real["age_s"] = max(0, int(now - updated)) if updated else None
                real.setdefault("from", FROM_HEADERS)
                for window in real.get("windows") or []:
                    reset = window.get("reset_at")
                    window["resets_in_s"] = max(0, int(reset - now)) if reset else None
                    window["stale"] = bool(reset and reset <= now)
            out[name] = row
        return out

    def over_threshold(self, backend: str, threshold: float) -> tuple[float, float] | None:
        """(used percent, seconds to reset) of the fullest live real window at or over it."""
        now = self._clock()
        with self._lock:
            row = self._load()["backends"].get(backend)
            real = row.get("real") if isinstance(row, dict) else None
            windows = real.get("windows") if isinstance(real, dict) else None
        best: tuple[float, float] | None = None
        for window in windows or []:
            used, reset = window.get("used_percent"), window.get("reset_at")
            if not isinstance(used, int | float) or used < threshold or not reset or reset <= now:
                continue
            if best is None or used > best[0]:
                best = (float(used), float(reset - now))
        return best


def level(percent: float | None) -> str:
    if percent is None:
        return "unknown"
    if percent >= CRITICAL_PERCENT:
        return "critical"
    if percent >= WARN_PERCENT:
        return "warn"
    return "ok"


_store: UsageStore | None = None
_store_lock = threading.Lock()


def store() -> UsageStore:
    """The process store. Re-created when the configured path changes (tests)."""
    global _store
    with _store_lock:
        if _store is None or _store.path != usage_path():
            _store = UsageStore(usage_path())
        return _store
