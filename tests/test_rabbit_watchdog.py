"""Watchdog behaviour with an injected clock and a fake launchctl. Nothing real is run.

Fixtures in ``tests/fixtures/launchctl`` are real ``launchctl print gui/<uid>/<label>``
captures from a Mac (environment blocks stripped, home paths scrubbed). Crash cases do not
occur in the captures, so they are DERIVED here by minimally editing a copy of the real
not-running capture; each derived case is marked as such.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nexus import rabbit_watchdog as rw
from nexus.app import create_app
from nexus.service import LAUNCHD_LABEL, LEGACY_LAUNCHD_LABEL, CommandResult, door_environment

FIXTURES = Path(__file__).parent / "fixtures" / "launchctl"
RUNNING = (FIXTURES / "rabbit-agent-running.txt").read_text()
CLEAN_EXIT = (FIXTURES / "not-running-clean-exit.txt").read_text()
JETSAM = (FIXTURES / "not-running-jetsam-no-exit-code.txt").read_text()
NOT_LOADED_STDERR = "\n".join((FIXTURES / "not-loaded.txt").read_text().splitlines()[:-1])
NOT_LOADED_EXIT = 113


def derived(base: str, old: str, new: str | None) -> str:
    """A copy of a real capture with one line replaced (or dropped when ``new`` is None)."""
    assert old in base
    return base.replace(old, new if new is not None else "", 1)


def exit_code(value: str) -> str:
    return derived(CLEAN_EXIT, "\tlast exit code = 0\n", f"\tlast exit code = {value}\n")


def terminated(value: str) -> str:
    return derived(CLEAN_EXIT, "\tlast exit code = 0\n", f"\tlast terminating signal = {value}\n")


def with_state(base: str, value: str) -> str:
    return derived(base, "\tstate = not running\n", f"\tstate = {value}\n")


CRASH_CASES = {
    "exit 1": exit_code("1"),
    "exit 78 named": exit_code("78: EX_CONFIG"),
    "exit -1": exit_code("-1"),
    "killed 9": terminated("Killed: 9"),
    "segv 11": terminated("Segmentation fault: 11"),
}
NO_ACTION_CASES = {
    "real clean exit": CLEAN_EXIT,
    "real jetsam, no exit code": JETSAM,
    "sigterm": terminated("Terminated: 15"),
    "sigint": terminated("Interrupt: 2"),
    "sighup": terminated("Hangup: 1"),
    "unparseable signal": terminated("???"),
    "unparseable signal number": terminated("Killed: nine"),
    "unparseable exit code": exit_code("oops"),
    "non-numeric exit code": exit_code("x"),
    "real running": RUNNING,
    "running with stale crash code": with_state(exit_code("1"), "running"),
    "spawn scheduled with stale crash code": with_state(exit_code("1"), "spawn scheduled"),
    "xpcproxy with stale crash code": with_state(exit_code("1"), "xpcproxy"),
    "future state value": with_state(exit_code("1"), "frobnicating"),
    "state not running with a suffix": with_state(exit_code("1"), "not running (throttled)"),
    "state line missing": derived(exit_code("1"), "\tstate = not running\n", None),
    "state only nested": derived(
        exit_code("1"), "\tstate = not running\n", "\t\tstate = not running\n"
    ),
    "empty output": "",
    "garbage": "Bad request.\n",
}


class FakeLaunchctl:
    def __init__(
        self,
        output: str = RUNNING,
        *,
        print_rc: int = 0,
        print_err: str = "",
        kick_rc: int = 0,
        timeout_on: str | None = None,
    ) -> None:
        self.output = output
        self.print_rc = print_rc
        self.print_err = print_err
        self.kick_rc = kick_rc
        self.timeout_on = timeout_on
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        if argv[1] == self.timeout_on:
            raise rw.CommandTimeout
        if argv[1] == "print":
            return CommandResult(self.print_rc, self.output, self.print_err)
        return CommandResult(self.kick_rc, "", "")

    @property
    def kicks(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "kickstart"]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_WATCHDOG", "on")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "watchdog-state.json"


def make(fake, state: Path, clock: Clock | None = None, platform: str = "darwin"):
    return rw.RabbitWatchdog(
        run=fake, clock=clock or Clock(), platform=platform, uid=501, state_path=state
    )


def run_to_restart(dog: rw.RabbitWatchdog, clock: Clock) -> str | None:
    dog.check()
    clock.now += 30
    return dog.check()


def test_off_by_default_runs_nothing(state: Path) -> None:
    fake = FakeLaunchctl(CRASH_CASES["exit 1"])
    dog = make(fake, state)
    for _ in range(5):
        assert dog.check() is None
    assert fake.calls == []
    assert not state.exists()


def test_enabled_off_macos_runs_nothing(on: None, state: Path) -> None:
    fake = FakeLaunchctl(CRASH_CASES["exit 1"])
    dog = make(fake, state, platform="linux")
    dog.check()
    dog.check()
    assert fake.calls == []
    assert dog.snapshot()["enabled"] is False
    assert dog.snapshot()["last_reason"] == "unsupported"


def test_healthy_idle_never_restarts(on: None, state: Path) -> None:
    fake, clock = FakeLaunchctl(RUNNING), Clock()
    dog = make(fake, state, clock)
    for _ in range(500):
        clock.now += 30
        assert dog.check() == "running"
    assert fake.kicks == []
    assert not state.exists()


@pytest.mark.parametrize("name", NO_ACTION_CASES)
def test_everything_else_is_left_alone(on: None, state: Path, name: str) -> None:
    fake = FakeLaunchctl(NO_ACTION_CASES[name])
    dog = make(fake, state)
    for _ in range(4):
        dog.check()
    assert fake.kicks == []
    assert dog.restart_count == 0
    assert not state.exists()


def test_reasons_for_real_captures(on: None, state: Path) -> None:
    assert make(FakeLaunchctl(RUNNING), state).check() == "running"
    assert make(FakeLaunchctl(CLEAN_EXIT), state).check() == "stopped_cleanly"
    assert make(FakeLaunchctl(JETSAM), state).check() == "stopped_cleanly"
    assert make(FakeLaunchctl(with_state(exit_code("1"), "spawn scheduled")), state).check() == (
        "unrecognised_launchctl_output"
    )


def test_not_loaded_is_logged_once_and_does_nothing(
    on: None, state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeLaunchctl(print_rc=NOT_LOADED_EXIT, print_err=NOT_LOADED_STDERR)
    dog = make(fake, state)
    with caplog.at_level("INFO", logger="nexus"):
        assert [dog.check() for _ in range(4)] == ["not_loaded"] * 4
    assert fake.kicks == []
    assert [r.getMessage() for r in caplog.records].count(
        "rabbit-agent watchdog: no launchd job with that label; no action"
    ) == 1


def test_unrecognised_output_is_logged_once(
    on: None, state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dog = make(FakeLaunchctl("nonsense"), state)
    with caplog.at_level("INFO", logger="nexus"):
        for _ in range(3):
            dog.check()
    assert len([r for r in caplog.records if "not recognised" in r.getMessage()]) == 1


@pytest.mark.parametrize("name", CRASH_CASES)
def test_crash_restarts_on_second_check_only(on: None, state: Path, name: str) -> None:
    fake, clock = FakeLaunchctl(CRASH_CASES[name]), Clock()
    dog = make(fake, state, clock)
    assert dog.check() == "crashed_waiting"
    assert fake.kicks == []
    clock.now += 30
    assert dog.check() == "crashed_restarted"
    assert fake.kicks == [["launchctl", "kickstart", "gui/501/com.rabbit.rabbit-agent"]]
    snap = dog.snapshot()
    assert snap["restart_count"] == 1
    assert snap["last_restart"] == rw._iso(clock.now)
    assert snap["last_check"] == rw._iso(clock.now)


def test_recovery_between_checks_resets_the_wait(on: None, state: Path) -> None:
    fake = FakeLaunchctl(CRASH_CASES["exit 1"])
    dog = make(fake, state)
    dog.check()
    fake.output = RUNNING
    dog.check()
    fake.output = CRASH_CASES["exit 1"]
    assert dog.check() == "crashed_waiting"
    assert fake.kicks == []


def test_at_most_three_restarts_per_hour(
    on: None, state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake, clock = FakeLaunchctl(CRASH_CASES["exit 1"]), Clock()
    dog = make(fake, state, clock)
    with caplog.at_level("INFO", logger="nexus"):
        for _ in range(100):
            clock.now += 30
            dog.check()
    assert len(fake.kicks) == rw.MAX_RESTARTS
    assert dog.last_reason == "backoff"
    lines = [r.getMessage() for r in caplog.records if "rabbit-agent" in r.getMessage()]
    assert len(lines) == 2 * rw.MAX_RESTARTS + 2
    assert all("\n" not in line for line in lines)
    clock.now += rw.WINDOW_SECONDS
    dog.check()
    assert len(fake.kicks) == rw.MAX_RESTARTS + 1


def test_cap_survives_a_new_watchdog_sharing_the_state_file(on: None, state: Path) -> None:
    fake, clock = FakeLaunchctl(CRASH_CASES["exit 1"]), Clock()
    first = make(fake, state, clock)
    for _ in range(2 * rw.MAX_RESTARTS):
        clock.now += 30
        first.check()
    assert len(fake.kicks) == rw.MAX_RESTARTS
    second = make(fake, state, clock)
    for _ in range(10):
        clock.now += 30
        second.check()
    assert len(fake.kicks) == rw.MAX_RESTARTS
    assert second.last_reason == "backoff"
    clock.now += rw.WINDOW_SECONDS
    assert second.check() == "crashed_restarted"
    assert len(fake.kicks) == rw.MAX_RESTARTS + 1


def test_state_file_is_private_atomic_and_holds_only_times(on: None, state: Path) -> None:
    clock = Clock()
    run_to_restart(make(FakeLaunchctl(CRASH_CASES["exit 1"]), state, clock), clock)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert json.loads(state.read_text()) == {"restarts": [clock.now]}
    assert [p.name for p in state.parent.iterdir()] == [state.name]


@pytest.mark.parametrize("content", ["not json", "[]", '{"restarts": "x"}', '{"restarts": [true]}'])
def test_unreadable_state_means_no_restart(
    on: None, state: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    state.write_text(content)
    fake, clock = FakeLaunchctl(CRASH_CASES["exit 1"]), Clock()
    dog = make(fake, state, clock)
    with caplog.at_level("INFO", logger="nexus"):
        for _ in range(6):
            clock.now += 30
            dog.check()
    assert fake.kicks == []
    assert dog.last_reason == "state_unavailable"
    assert state.read_text() == content
    assert len([r for r in caplog.records if "restart-times file" in r.getMessage()]) == 1


def test_unwritable_state_means_no_restart(on: None, tmp_path: Path) -> None:
    fake, clock = FakeLaunchctl(CRASH_CASES["exit 1"]), Clock()
    dog = make(fake, tmp_path / "missing-dir" / "state.json", clock)
    for _ in range(4):
        clock.now += 30
        dog.check()
    assert fake.kicks == []
    assert dog.last_reason == "state_unavailable"


def test_failed_kickstart_counts_toward_the_limit(on: None, state: Path) -> None:
    fake, clock = FakeLaunchctl(CRASH_CASES["exit 1"], kick_rc=5), Clock()
    dog = make(fake, state, clock)
    for _ in range(40):
        clock.now += 30
        dog.check()
    assert len(fake.kicks) == rw.MAX_RESTARTS
    assert dog.restart_count == 0
    assert dog.restarted_at is None


def test_print_timeout_is_no_action_and_logged_once(
    on: None, state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeLaunchctl(CRASH_CASES["exit 1"], timeout_on="print")
    dog = make(fake, state)
    with caplog.at_level("INFO", logger="nexus"):
        assert [dog.check() for _ in range(4)] == ["launchctl_timeout"] * 4
    assert fake.kicks == []
    assert len([r for r in caplog.records if "timed out" in r.getMessage()]) == 1
    assert dog.checked_at is not None


def test_kickstart_timeout_is_restart_failed(on: None, state: Path) -> None:
    fake, clock = FakeLaunchctl(CRASH_CASES["exit 1"], timeout_on="kickstart"), Clock()
    dog = make(fake, state, clock)
    assert run_to_restart(dog, clock) == "restart_failed"
    assert dog.restart_count == 0


def test_runner_uses_a_list_argv_a_timeout_and_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    assert rw.run_launchctl(["launchctl", "print", "x"]).stdout == "out"
    assert seen["argv"] == ["launchctl", "print", "x"]
    assert seen["kwargs"]["timeout"] == 10.0
    assert not seen["kwargs"].get("shell")

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(rw.subprocess, "run", hang)
    with pytest.raises(rw.CommandTimeout):
        rw.run_launchctl(["launchctl", "print", "x"])

    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(rw.subprocess, "run", missing)
    assert rw.run_launchctl(["launchctl", "print", "x"]).returncode == 127


def test_never_targets_this_bridge(
    on: None, state: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid = 4242424
    monkeypatch.setattr(os, "getpid", lambda: pid)
    fake, clock = FakeLaunchctl(), Clock()
    dog = make(fake, state, clock)
    for output in [*CRASH_CASES.values(), *NO_ACTION_CASES.values()]:
        fake.output = output
        for _ in range(4):
            clock.now += 30
            dog.check()
    assert fake.calls and fake.kicks
    forbidden_parts = {str(pid), "11435", LAUNCHD_LABEL, "-k", "-9", "kill", "bootout"}
    forbidden_substrings = ("11435", "os3-bridge", "grokbot", "kill", "bootout")
    for argv in fake.calls:
        assert argv[0] == "launchctl" and argv[1] in {"print", "kickstart"}
        assert argv[2] == "gui/501/com.rabbit.rabbit-agent" and len(argv) == 3
        assert not forbidden_parts & set(argv)
        assert not any(bad in part.lower() for part in argv for bad in forbidden_substrings)


@pytest.mark.parametrize(
    "label",
    [
        LAUNCHD_LABEL,
        LAUNCHD_LABEL.upper(),
        LEGACY_LAUNCHD_LABEL,
        "com.example.os3-bridge",
        "com.grokbot.anything",
        "../../etc",
        "a b",
        "-k",
        "x/y",
    ],
)
def test_own_or_odd_label_is_refused_logged_once_and_nothing_runs(
    on: None,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_AGENT_LABEL", label)
    fake = FakeLaunchctl(CRASH_CASES["exit 1"])
    dog = make(fake, state)
    with caplog.at_level("INFO", logger="nexus"):
        assert [dog.check() for _ in range(4)] == ["refused"] * 4
    assert fake.calls == []
    assert len([r for r in caplog.records if "refusing" in r.getMessage()]) == 1
    assert label not in caplog.text or label == ""


def test_label_override_is_used(on: None, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_AGENT_LABEL", "com.rabbit.other-agent")
    fake = FakeLaunchctl(RUNNING)
    make(fake, state).check()
    assert fake.calls == [["launchctl", "print", "gui/501/com.rabbit.other-agent"]]


def test_loop_survives_a_crashing_check_and_stops_on_cancel(on: None, state: Path) -> None:
    class Boom(rw.RabbitWatchdog):
        def check(self) -> str | None:
            raise RuntimeError("boom")

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise asyncio.CancelledError

    dog = Boom(run=FakeLaunchctl(), platform="darwin", uid=501, state_path=state)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rw.rabbit_watchdog_loop(dog, interval_seconds=7, sleep=fake_sleep))
    assert slept == [7, 7, 7]


def test_off_drops_the_watchdog_key_from_health_and_doctor() -> None:
    with TestClient(create_app()) as client:
        assert "watchdog" not in client.get("/health").json()
        assert "watchdog" not in client.get("/doctor").json()


def test_flag_on_adds_the_watchdog_object(on: None) -> None:
    with TestClient(create_app()) as client:
        for path in ("/health", "/doctor"):
            body = client.get(path).json()
            assert set(body["watchdog"]) == {
                "enabled",
                "last_check",
                "last_restart",
                "restart_count",
                "last_reason",
            }


def test_service_environment_passes_the_opt_in_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "OS3_BRIDGE_RABBIT_WATCHDOG" not in door_environment("openrouter")
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_WATCHDOG", "on")
    assert door_environment("openrouter")["OS3_BRIDGE_RABBIT_WATCHDOG"] == "on"
