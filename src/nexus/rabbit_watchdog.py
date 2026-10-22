"""Restart Rabbit's own ``rabbit-agent`` LaunchAgent after it has crashed. Off by default.

OS3 reaches this door through ``rabbit-agent``. If that process dies, OS3 stops working
while ``/health`` still says ready. launchd owns the agent, so the door does not run a
second one: it asks launchd about the existing job and, when the job is loaded, not
running, and its last end was a crash, it runs ``launchctl kickstart`` on that job.

What it can tell from outside is a crashed agent. It cannot tell an idle tunnel from a
stuck one: rabbit-agent has no local health probe this repo can rely on, and "no OS3
traffic" looks the same whether OS3 is idle or cut off. So it never acts on traffic, and
an agent that is running is left alone, however quiet.

``launchctl print`` output is not an API, so parsing fails safe: it acts only on a job
whose top-level ``state`` is exactly ``not running`` and whose last end it can read as a
crash. A missing, odd, or unparseable line means no action. Argv is built in one place
and is only ``launchctl print`` or ``launchctl kickstart`` on ``gui/<uid>/<agent label>``:
no ``-k``, no pid, no port, no pattern, and a label that could be this bridge's own is
refused. launchctl output can carry the job's environment, so only parsed fields are
stored or logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nexus.config import (
    rabbit_agent_label,
    rabbit_watchdog_enabled,
    rabbit_watchdog_state_path,
)
from nexus.service import LAUNCHD_LABEL, LEGACY_LAUNCHD_LABEL, CommandResult

logger = logging.getLogger("nexus")

CHECK_INTERVAL_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 10.0
MAX_RESTARTS = 3
WINDOW_SECONDS = 3600.0
NOT_FOUND_EXIT = 113

# SIGHUP, SIGINT, SIGTERM: how a person or `launchctl stop` asks a job to end.
_POLITE_SIGNALS = {1, 2, 15}
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class UnsafeTarget(ValueError):
    """The configured agent label could be this bridge. Nothing is run."""


class CommandTimeout(Exception):
    """launchctl did not answer in time."""


class StateUnavailable(Exception):
    """The restart-times file could not be read or written."""


def validate_label(label: str) -> str:
    if not _LABEL.fullmatch(label):
        raise UnsafeTarget("agent label must be a plain launchd label")
    lowered = label.lower()
    own = {LAUNCHD_LABEL.lower(), LEGACY_LAUNCHD_LABEL.lower()}
    if lowered in own or any(part in lowered for part in ("os3-bridge", "grokbot", "nexus")):
        raise UnsafeTarget("agent label must not be this bridge's own LaunchAgent")
    return label


def run_launchctl(argv: list[str]) -> CommandResult:
    """The only place the watchdog starts a process. List argv, no shell, bounded time."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout from exc
    except OSError as exc:
        return CommandResult(127, "", f"{argv[0]}: {exc.strerror or 'could not be run'}")
    return CommandResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class AgentJob:
    """``known`` is true only for a job launchctl reports as exactly ``not running``."""

    not_running: bool
    crashed: bool
    known: bool


def _field(text: str, key: str) -> str | None:
    """A top-level field: one tab of indent. Nested blocks use two and never match."""
    match = re.search(rf"^\t{re.escape(key)} = (.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _exit_code(value: str) -> int | None:
    """``1`` or a named one such as ``78: EX_CONFIG``. Anything else is unreadable."""
    match = re.fullmatch(r"(-?\d+)(?::\s*\S.*)?", value)
    return int(match.group(1)) if match else None


def _signal_number(value: str) -> int | None:
    """``Killed: 9``. Anything else is unreadable."""
    match = re.fullmatch(r"[^:]+:\s*(\d+)", value)
    return int(match.group(1)) if match else None


def parse_agent_job(stdout: str) -> AgentJob:
    """Read ``launchctl print`` output. Only ``state = not running`` can ever be a crash."""
    state = _field(stdout, "state")
    if state is None:
        return AgentJob(not_running=False, crashed=False, known=False)
    if state != "not running":
        return AgentJob(not_running=False, crashed=False, known=state == "running")
    crashed = False
    code = _field(stdout, "last exit code")
    signal = _field(stdout, "last terminating signal")
    if code is not None:
        number = _exit_code(code)
        crashed = number is not None and number != 0
    elif signal is not None:
        number = _signal_number(signal)
        crashed = number is not None and number not in _POLITE_SIGNALS
    return AgentJob(not_running=True, crashed=crashed, known=True)


def _iso(stamp: float | None) -> str | None:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_restarts(path: Path) -> list[float]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StateUnavailable from exc
    try:
        stamps = json.loads(raw)["restarts"]
    except (ValueError, KeyError, TypeError) as exc:
        raise StateUnavailable from exc
    if not isinstance(stamps, list) or not all(
        isinstance(t, int | float) and not isinstance(t, bool) for t in stamps
    ):
        raise StateUnavailable
    return [float(t) for t in stamps]


def _write_restarts(path: Path, stamps: list[float]) -> None:
    """Mode 0600 from creation, then an atomic rename over the old file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"restarts": stamps}, handle)
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise StateUnavailable from exc


class RabbitWatchdog:
    """``check`` is blocking; the loop runs it in a thread. Time and commands are injected."""

    def __init__(
        self,
        *,
        run: Callable[[list[str]], CommandResult] = run_launchctl,
        clock: Callable[[], float] = time.time,
        platform: str | None = None,
        uid: int | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._run = run
        self._clock = clock
        self._platform = sys.platform if platform is None else platform
        self._uid = uid
        self._state_path = state_path
        self.checked_at: float | None = None
        self.restarted_at: float | None = None
        self.restart_count = 0
        self.last_reason: str | None = None
        self._down_seen = False

    @property
    def enabled(self) -> bool:
        return self._platform == "darwin" and rabbit_watchdog_enabled()

    def _argv(self, verb: str, label: str) -> list[str]:
        uid = os.getuid() if self._uid is None else self._uid
        return ["launchctl", verb, f"gui/{uid}/{label}"]

    def _note(self, reason: str, message: str | None = None, *, level: int = logging.WARNING):
        """Record the reason; log one line when it is newly entered."""
        if message and reason != self.last_reason:
            logger.log(level, "rabbit-agent watchdog: %s", message)
        self.last_reason = reason
        return reason

    def check(self) -> str | None:
        """Look once and, if every rule holds, restart once. Returns the reason, if any."""
        if not self.enabled:
            return None
        try:
            label = validate_label(rabbit_agent_label())
        except UnsafeTarget as exc:
            return self._note("refused", f"refusing the configured agent label ({exc})")
        self.checked_at = now = self._clock()
        try:
            printed = self._run(self._argv("print", label))
        except CommandTimeout:
            self._down_seen = False
            return self._note("launchctl_timeout", "launchctl print timed out; no action")
        if printed.returncode != 0:
            self._down_seen = False
            if printed.returncode == NOT_FOUND_EXIT:
                return self._note("not_loaded", "no launchd job with that label; no action")
            return self._note("launchctl_failed", f"launchctl print failed ({printed.returncode})")
        job = parse_agent_job(printed.stdout)
        if not job.known:
            self._down_seen = False
            return self._note(
                "unrecognised_launchctl_output", "launchctl print output not recognised; no action"
            )
        if not job.crashed:
            self._down_seen = False
            return self._note("stopped_cleanly" if job.not_running else "running")
        if not self._down_seen:
            self._down_seen = True
            return self._note("crashed_waiting", "agent not running after a crash; checking again")
        self._restart(now, label)
        return self.last_reason

    def _restart(self, now: float, label: str) -> None:
        path = self._state_path or rabbit_watchdog_state_path()
        try:
            recent = [t for t in _read_restarts(path) if now - t < WINDOW_SECONDS]
            if len(recent) >= MAX_RESTARTS:
                self._note("backoff", f"{MAX_RESTARTS} restarts in the last hour; not retrying")
                return
            _write_restarts(path, [*recent, now])
        except StateUnavailable:
            self._note("state_unavailable", "restart-times file unusable; not restarting")
            return
        try:
            result = self._run(self._argv("kickstart", label))
        except CommandTimeout:
            self._note("restart_failed", "launchctl kickstart timed out")
            return
        if result.returncode != 0:
            self._note("restart_failed", f"launchctl kickstart failed ({result.returncode})")
            return
        self.restart_count += 1
        self.restarted_at = now
        self._down_seen = False
        self._note(
            "crashed_restarted", "agent had crashed; restarted with kickstart", level=logging.INFO
        )

    def snapshot(self) -> dict[str, object]:
        """The ``watchdog`` object in /health. Memory only, never launchctl."""
        return {
            "enabled": self.enabled,
            "last_check": _iso(self.checked_at),
            "last_restart": _iso(self.restarted_at),
            "restart_count": self.restart_count,
            "last_reason": self.last_reason if self.enabled else "unsupported",
        }


WATCHDOG = RabbitWatchdog()


async def rabbit_watchdog_loop(
    watchdog: RabbitWatchdog | None = None,
    *,
    interval_seconds: float = CHECK_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Check on a timer for as long as the door runs. A failed check never ends the loop."""
    active = WATCHDOG if watchdog is None else watchdog
    while True:
        try:
            await asyncio.to_thread(active.check)
        except Exception:
            logger.exception("rabbit-agent check crashed")
        await sleep(interval_seconds)
