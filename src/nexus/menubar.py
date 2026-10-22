"""Optional menu-bar status for the door. Nothing in the serving path imports it.

`./nexus menubar` puts a status item in the macOS menu bar that polls
`/health` and can restart the packaged service. The door does not need it: only
that subcommand imports this module, and pyobjc is an optional extra, so
`./nexus start` behaves the same with or without it installed.

Status is up or down, and that is all. `/health` `ok: true` means the process
answered, not that the backend is `ready`. The poll sends no bridge key, because
`/health` is unauthenticated and the door is on loopback.

Restart runs `./nexus stop` then `./nexus start` through
`nexus.cli.main`. Those are the subcommands the CLI already performs, so
the menu is not a second lifecycle path and never calls launchctl itself.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from nexus.config import configured_host, configured_port, environ_get

DEFAULT_INTERVAL = 5.0
_MIN_INTERVAL = 1.0
_MAX_INTERVAL = 3600.0
_TIMEOUT = 2.0
_DETAIL_LIMIT = 60
INTERVAL_ENV = "NEXUS_MENUBAR_INTERVAL"
# A restart is not instantaneous: bootout is async and the new listener takes a
# moment to bind. These bound how long the menu waits before showing the truth.
_STOP_TIMEOUT = 10.0
_RESTART_TIMEOUT = 20.0


class MenuBarUnavailable(Exception):
    """The menu-bar shell could not start. The message must not contain a secret."""


@dataclass(frozen=True)
class DoorState:
    """One reading of the door. `detail` is a short, secret-free label."""

    up: bool
    detail: str
    backend: str = ""
    version: str = ""


Fetch = Callable[[str, float], tuple[int, str]]
Runner = Callable[[list[str]], tuple[int, str]]


def _short(text: object, limit: int = _DETAIL_LIMIT) -> str:
    line = " ".join(str(text).split())
    if len(line) <= limit:
        return line
    return line[: limit - 1] + "…"


def health_url(host: str | None = None, port: int | None = None) -> str:
    """The door's own health endpoint. No key is attached to this request."""
    return f"http://{host or configured_host()}:{port or configured_port()}/health"


def fetch_health(url: str, timeout: float) -> tuple[int, str]:
    """Read `/health` over loopback. The caller turns the result into a DoorState."""
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url)
    return response.status_code, response.text


def classify(status_code: int, body: str) -> DoorState:
    """`ok: true` is up. A non-200, a bad body, and `ok: false` are all down."""
    if status_code != 200:
        return DoorState(up=False, detail=f"HTTP {status_code}")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return DoorState(up=False, detail="unreadable /health")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return DoorState(up=False, detail="not ok")
    backend = payload.get("backend")
    version = payload.get("version")
    label = " ".join(str(item) for item in (backend, version) if item)
    return DoorState(
        up=True,
        detail=_short(label or "up"),
        backend=backend if isinstance(backend, str) else "",
        version=version if isinstance(version, str) else "",
    )


def probe_health(
    host: str | None = None,
    port: int | None = None,
    *,
    fetch: Fetch = fetch_health,
    timeout: float = _TIMEOUT,
) -> DoorState:
    """One door reading. A refused connection or a timeout is down, not an error."""
    try:
        status_code, body = fetch(health_url(host, port), timeout)
    except (httpx.HTTPError, OSError) as exc:
        return DoorState(up=False, detail=_short(type(exc).__name__))
    return classify(status_code, body)


def status_title(state: DoorState) -> str:
    """The menu-bar text. Two glyphs, so it reads at a glance."""
    return "nexus ●" if state.up else "nexus ○"


def _cli_runner(args: list[str]) -> tuple[int, str]:
    """Run one `nexus` subcommand in-process and capture what it printed."""
    from nexus.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(args)
    return code, f"{out.getvalue()}{err.getvalue()}".strip()


def _wait_for(
    wanted: bool,
    timeout: float,
    poll: Callable[[], DoorState],
    *,
    delay: float = 0.25,
) -> DoorState:
    """Poll until `up` is `wanted`, or the timeout runs out. Last read wins."""
    deadline = time.monotonic() + timeout
    state = poll()
    while state.up != wanted and time.monotonic() < deadline:
        time.sleep(delay)
        state = poll()
    return state


def restart_door(
    runner: Runner = _cli_runner,
    probe: Callable[[], DoorState] | None = None,
    *,
    ready_timeout: float = _RESTART_TIMEOUT,
) -> DoorState:
    """Reload the packaged service, then read the door back.

    Stop runs first, and a failed stop skips the start, so a refused restart does
    not leave the door unloaded. Both halves are the CLI's own subcommands.

    `stop` and `start` are not synchronous: the old process keeps answering for a
    moment after bootout, and launchctl reports success before the new one binds.
    So wait for the door to actually stop answering before starting, then wait for
    it to answer again. Without the first wait the read below can catch the
    outgoing process and report a restart that never happened.
    """
    read = probe_health if probe is None else probe

    code, text = runner(["stop"])
    if code != 0:
        return DoorState(up=False, detail=_short(text or "stop failed"))
    _wait_for(False, min(ready_timeout, _STOP_TIMEOUT), read)

    code, text = runner(["start"])
    if code != 0:
        return DoorState(up=False, detail=_short(text or "start failed"))
    return _wait_for(True, ready_timeout, read)


def poll_interval(raw: str | None = None) -> float:
    """Seconds between polls. Unset or nonsensical input is the default."""
    value = raw if raw is not None else (environ_get(INTERVAL_ENV) or "")
    try:
        seconds = float(value)
    except ValueError:
        return DEFAULT_INTERVAL
    if math.isnan(seconds):
        return DEFAULT_INTERVAL
    return max(_MIN_INTERVAL, min(seconds, _MAX_INTERVAL))


class DoorPoller:
    """Polls on a worker thread. The UI thread only ever reads `latest`.

    `on_state` is called with every new reading, on the polling thread. Anything
    it touches has to hop to the main thread itself.
    """

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        host: str | None = None,
        port: int | None = None,
        fetch: Fetch = fetch_health,
        probe: Callable[[], DoorState] | None = None,
        on_state: Callable[[DoorState], None] | None = None,
    ) -> None:
        self.interval = interval
        self.host = host
        self.port = port
        self._fetch = fetch
        self._probe = probe
        self._on_state = on_state
        self._lock = threading.Lock()
        self._state = DoorState(up=False, detail="checking…")
        self._stop = threading.Event()

    def latest(self) -> DoorState:
        with self._lock:
            return self._state

    def _set(self, state: DoorState) -> DoorState:
        with self._lock:
            self._state = state
        if self._on_state is not None:
            with contextlib.suppress(Exception):
                self._on_state(state)
        return state

    def check(self) -> DoorState:
        if self._probe is not None:
            return self._set(self._probe())
        return self._set(probe_health(self.host, self.port, fetch=self._fetch))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.check()
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self._loop, name="os3-menubar-poll", daemon=True).start()

    def close(self) -> None:
        self._stop.set()


def missing_pyobjc_message(detail: str = "") -> str:
    lines = [
        "The menu bar needs pyobjc, which is not installed.",
        "  make menubar-install",
        "The door does not need it and is unaffected.",
    ]
    if detail:
        lines.append(f"({detail})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for `./nexus menubar`. macOS only."""
    if sys.platform != "darwin":
        print("The nexus menu bar is macOS only. The door runs on Linux too.")
        return 1
    interval = poll_interval()
    for index, item in enumerate(argv or []):
        if item == "--interval" and index + 1 < len(argv or []):
            interval = poll_interval(argv[index + 1])
    try:
        from nexus.menubar_app import run
    except ImportError as exc:
        print(missing_pyobjc_message(str(exc)))
        return 1
    try:
        run(interval)
    except MenuBarUnavailable as exc:
        print(missing_pyobjc_message(str(exc)))
        return 1
    return 0
