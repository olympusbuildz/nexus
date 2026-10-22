"""Menu-bar status reads /health and reuses the CLI's own start and stop."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from nexus import menubar, menubar_app
from nexus.menubar import (
    DEFAULT_INTERVAL,
    DoorPoller,
    DoorState,
    MenuBarUnavailable,
    classify,
    health_url,
    poll_interval,
    probe_health,
    restart_door,
    status_title,
)


def _ok_body(backend: str = "openrouter", version: str = "0.7.0") -> str:
    return (
        f'{{"ok": true, "service": "os3-bridge", "version": "{version}", "backend": "{backend}"}}'
    )


def test_health_url_targets_the_door() -> None:
    assert health_url() == "http://127.0.0.1:11435/health"
    assert health_url("127.0.0.1", 9999) == "http://127.0.0.1:9999/health"


def test_ok_body_is_up() -> None:
    state = classify(200, _ok_body())
    assert state.up is True
    assert state.backend == "openrouter"
    assert state.version == "0.7.0"
    assert status_title(state) == "nexus ●"


@pytest.mark.parametrize(
    ("status_code", "body", "detail"),
    [
        (503, "{}", "HTTP 503"),
        (200, "not json", "unreadable /health"),
        (200, "[]", "not ok"),
        (200, '{"ok": false}', "not ok"),
    ],
)
def test_anything_else_is_down(status_code: int, body: str, detail: str) -> None:
    state = classify(status_code, body)
    assert state.up is False
    assert state.detail == detail
    assert status_title(state) == "nexus ○"


def test_refused_connection_is_down_not_an_error() -> None:
    def refuse(_url: str, _timeout: float) -> tuple[int, str]:
        raise httpx.ConnectError("connection refused")

    state = probe_health(fetch=refuse)
    assert state.up is False
    assert state.detail == "ConnectError"


def test_probe_asks_the_door_and_nothing_else() -> None:
    seen: dict[str, str] = {}

    def fetch(url: str, _timeout: float) -> tuple[int, str]:
        seen["url"] = url
        return 200, _ok_body()

    probe_health(fetch=fetch)
    assert seen["url"] == "http://127.0.0.1:11435/health"


def test_restart_stops_then_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls: list[list[str]] = []
    order: list[str] = []

    def runner(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        return 0, "done"

    def probe() -> DoorState:
        phase = order[-1] if order else "start"
        return DoorState(up=phase == "start", detail="new")

    def traced(args: list[str]) -> tuple[int, str]:
        order.append(args[0])
        return runner(args)

    state = restart_door(runner=traced, probe=probe)
    assert calls == [["stop"], ["start"]]
    assert state.up is True


def test_failed_stop_skips_start() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        return 1, "port check failed"

    state = restart_door(runner=runner)
    assert calls == [["stop"]]
    assert state.up is False
    assert state.detail == "port check failed"


def test_restart_waits_for_the_old_process_to_go_away(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outgoing process keeps answering after bootout; that is not a restart."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    readings = iter(
        [
            DoorState(up=True, detail="old"),
            DoorState(up=True, detail="old"),
            DoorState(up=True, detail="old"),
            DoorState(up=False, detail="ConnectError"),
            DoorState(up=True, detail="new"),
        ]
    )
    seen: list[str] = []

    def runner(args: list[str]) -> tuple[int, str]:
        seen.append(args[0])
        return 0, "done"

    def probe() -> DoorState:
        return next(readings)

    state = restart_door(runner=runner, probe=probe)
    assert seen == ["stop", "start"]
    assert state.up is True
    assert state.detail == "new"


def test_restart_reports_down_when_the_door_never_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def tick(_seconds: float) -> None:
        clock[0] += 1.0

    monkeypatch.setattr(time, "sleep", tick)

    def runner(_args: list[str]) -> tuple[int, str]:
        return 0, "done"

    state = restart_door(runner=runner, probe=lambda: DoorState(up=False, detail="down"))
    assert state.up is False


def test_poller_publishes_every_reading() -> None:
    seen: list[DoorState] = []
    poller = DoorPoller(probe=lambda: DoorState(up=True, detail="up"), on_state=seen.append)
    poller.check()
    poller.check()
    assert poller.latest() == DoorState(up=True, detail="up")
    assert len(seen) == 2


def test_interval_parsing() -> None:
    assert poll_interval("") == DEFAULT_INTERVAL
    assert poll_interval("nonsense") == DEFAULT_INTERVAL
    assert poll_interval("nan") == DEFAULT_INTERVAL
    assert poll_interval("0.1") == 1.0
    assert poll_interval("30") == 30.0
    assert poll_interval("99999") == 3600.0


def test_missing_pyobjc_says_so_and_leaves_the_door_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without pyobjc the subcommand explains the optional extra, exit 1."""

    def unavailable(_interval: float | None = None) -> None:
        raise MenuBarUnavailable("No module named 'AppKit'")

    monkeypatch.setattr(menubar_app, "run", unavailable)
    assert menubar.main([]) == 1


def test_missing_pyobjc_message_is_actionable() -> None:
    text = menubar.missing_pyobjc_message("No module named 'AppKit'")
    assert "make menubar-install" in text
    assert "does not need it" in text


def test_serving_path_does_not_import_the_menu_bar() -> None:
    """`status` must reach the packaged service without loading the AppKit shell.

    Run in a fresh interpreter, because this test file imports the menu bar
    itself and a same-process check would only see its own imports.
    """
    script = (
        "import sys\n"
        "import nexus.cli as cli\n"
        "reached = []\n"
        "def stop():\n"
        "    reached.append(1)\n"
        "    raise SystemExit(0)\n"
        "cli.build_platform = stop\n"
        "try:\n"
        "    cli.main(['status'])\n"
        "except SystemExit as exc:\n"
        "    if exc.code != 0:\n"
        "        raise SystemExit(f'status exited {exc.code!r} before the service')\n"
        "assert reached, 'status never reached build_platform'\n"
        "loaded = [m for m in ('nexus.menubar', 'nexus.menubar_app', 'AppKit')\n"
        "          if m in sys.modules]\n"
        "raise SystemExit(f'loaded={loaded}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
    )
    assert result.returncode == 1, result.stdout
    assert "loaded=[]" in result.stderr, result.stdout
