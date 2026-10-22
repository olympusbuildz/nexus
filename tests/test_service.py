"""Service helpers refuse a taken door port and do not signal foreign processes."""

from __future__ import annotations

import os
import plistlib
import signal
import sys
from pathlib import Path

import pytest
from nexus.cli import main
from nexus.config import repo_root
from nexus.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    CommandResult,
    LaunchdPlatform,
    Listener,
    PackagedService,
    PortCheckError,
    SystemdPlatform,
    build_platform,
    collect_listeners,
    door_environment,
    execute_start,
    execute_stop,
    listeners_from_proc,
    parse_launchctl_print,
    parse_lsof_field_output,
    parse_systemctl_show,
    port_check_failure,
    render_installed_unit,
    render_unit,
    service_path,
)


class FakePlatform:
    kind = "launchd"
    label = LAUNCHD_LABEL

    def __init__(self, listeners: list[Listener], service: PackagedService) -> None:
        self._listeners = list(listeners)
        self.service = service
        self.events: list[str] = []
        self.unit: str | None = None
        self.fail_port = False

    def listeners(self) -> list[Listener]:
        if self.fail_port:
            raise PortCheckError(port_check_failure())
        return list(self._listeners)

    def packaged(self) -> PackagedService:
        return self.service

    def install_and_start(self, unit_text: str) -> None:
        self.events.append("start")
        self.unit = unit_text

    def stop_and_remove(self) -> None:
        self.events.append("stop")
        pid = self.service.pid
        self.service = PackagedService(installed=False, running=False, pid=None)
        if pid is not None:
            self._listeners = [item for item in self._listeners if item.pid != pid]


def _patch_start(monkeypatch: pytest.MonkeyPatch, platform: FakePlatform) -> None:
    monkeypatch.setattr("nexus.cli.build_platform", lambda: platform)
    monkeypatch.setattr("nexus.cli.venv_python", lambda: Path("/tmp/os3-python"))


def test_lsof_and_proc_parsers(tmp_path: Path) -> None:
    text = "p7\ncother\np7\ncother\np8\ncpython\n"
    assert parse_lsof_field_output(text) == [
        Listener(7, "other"),
        Listener(8, "python"),
    ]
    assert parse_lsof_field_output("") == []

    proc = tmp_path / "proc"
    _proc_listener(proc)
    found = listeners_from_proc(proc, 11435)
    assert found == [Listener(42, "/usr/bin/python server.py")]
    (proc / "42" / "fd" / "3").unlink()
    (proc / "42" / "fd" / "3").symlink_to("socket:[1]")
    assert listeners_from_proc(proc, 11435) == [Listener(0, "unknown")]


def test_lsof_failure_does_not_fall_through(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _proc_listener(proc)

    def run(_argv: list[str]) -> CommandResult:
        return CommandResult(2, "", "lsof failed")

    with pytest.raises(PortCheckError):
        collect_listeners(11435, run=run, which=lambda _name: "/usr/bin/lsof", proc_root=proc)
    found = collect_listeners(11435, run=run, which=lambda _name: None, proc_root=proc)
    assert found == [Listener(42, "/usr/bin/python server.py")]


def test_execute_start_and_stop_leave_foreign_listeners() -> None:
    busy = FakePlatform([Listener(9, "other")], PackagedService(False, False, None))
    refused = execute_start(busy, "unit")
    assert refused.code == 1
    assert busy.events == []
    assert "pid 9" in refused.message
    assert "left running" in refused.message.lower()

    running = FakePlatform([Listener(4, "python")], PackagedService(True, True, 4))
    stayed = execute_start(running, "unit")
    assert stayed.code == 0
    assert running.events == []
    assert "Left it running" in stayed.message

    stale = FakePlatform([], PackagedService(True, False, None))
    started = execute_start(stale, "unit-text")
    assert started.code == 0
    assert stale.events == ["stop", "start"]
    assert stale.unit == "unit-text"

    failed = FakePlatform([], PackagedService(False, False, None))
    failed.fail_port = True
    blocked = execute_start(failed, "unit")
    assert blocked.code == 1
    assert failed.events == []
    assert "left unchanged" in blocked.message

    bare = FakePlatform([Listener(9, "other")], PackagedService(False, False, None))
    stopped = execute_stop(bare)
    assert stopped.code == 1
    assert bare.events == []
    assert "left running" in stopped.message.lower()

    ours = FakePlatform([Listener(4, "python")], PackagedService(True, True, 4))
    done = execute_stop(ours)
    assert done.code == 0
    assert ours.events == ["stop"]
    assert "Key files were kept" in done.message
    assert "not signaled" in done.message

    both = FakePlatform(
        [Listener(4, "python"), Listener(9, "other")],
        PackagedService(True, True, 4),
    )
    mixed = execute_stop(both)
    assert mixed.code == 0
    assert both.events == ["stop"]
    assert "pid 9" in mixed.message
    assert "left running" in mixed.message.lower()


def _foreground_probe(pid: int) -> tuple[str | None, Path | None]:
    root = repo_root()
    if pid == 21:
        return f"{root}/.venv/bin/python {root}/server.py", root
    if pid == 22:
        return "/other/checkout/.venv/bin/python /other/checkout/server.py", Path("/other/checkout")
    return "/usr/bin/some-other-program", Path("/")


class _ForegroundPlatform(FakePlatform):
    def __init__(self, listeners: list[Listener], *, exits: bool = True) -> None:
        super().__init__(listeners, PackagedService(False, False, None))
        self.exits = exits
        self.signals: list[tuple[int, int]] = []

    def terminate(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if self.exits:
            self._listeners = [item for item in self._listeners if item.pid != pid]


def test_stop_ends_a_foreground_run_from_this_checkout() -> None:
    platform = _ForegroundPlatform([Listener(21, "python")])
    done = execute_stop(
        platform,
        probe=_foreground_probe,
        terminate=platform.terminate,
        sleep=lambda _s: None,
    )
    assert done.code == 0
    assert platform.signals == [(21, signal.SIGTERM)]
    assert "Stopped the foreground nexus (pid 21)" in done.message
    assert "Refusing" not in done.message


def test_stop_never_signals_another_checkout_or_a_stranger() -> None:
    platform = _ForegroundPlatform([Listener(22, "python"), Listener(23, "other")])
    done = execute_stop(
        platform,
        probe=_foreground_probe,
        terminate=platform.terminate,
        sleep=lambda _s: None,
    )
    assert done.code == 1
    assert platform.signals == []
    assert "pid 22" in done.message
    assert "pid 23" in done.message
    assert "Refusing to start" not in done.message
    assert "Nothing was signaled" in done.message


def test_stop_reports_a_foreground_run_that_will_not_exit() -> None:
    platform = _ForegroundPlatform([Listener(21, "python")], exits=False)
    done = execute_stop(
        platform,
        probe=_foreground_probe,
        terminate=platform.terminate,
        sleep=lambda _s: None,
    )
    assert done.code == 1
    assert "did not exit after SIGTERM" in done.message


def test_unit_binds_loopback_and_skips_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-should-not-enter-unit")
    env = door_environment(
        "codex",
        model="gpt-5.6-sol",
        cli_path="/opt/My Tools/codex",
    )
    assert "OPENROUTER_API_KEY" not in env
    assert env["OS3_BRIDGE_HOST"] == "127.0.0.1"
    assert env["OS3_BRIDGE_PORT"] == "11435"
    assert env["OS3_BRIDGE_SERVICE"] == "1"
    launchd = render_installed_unit(
        "launchd",
        repo=Path("/tmp/a&b"),
        python=Path("/usr/bin/python3"),
        env=env,
    )
    systemd = render_installed_unit(
        "systemd",
        repo=Path("/tmp/a&b"),
        python=Path("/opt/My Tools/python"),
        env=env,
    )
    for text in (launchd, systemd):
        assert "127.0.0.1" in text
        assert "11435" in text
        assert "localhost" not in text
        assert "sk-or-should-not-enter-unit" not in text
        assert "OPENROUTER_API_KEY" not in text
        assert "__REPO__" not in text
        assert "__PYTHON__" not in text
        assert "__ENV_" not in text
    assert "/tmp/a&amp;b" in launchd
    assert "<string>run</string>" in launchd
    assert "<key>SuccessfulExit</key>" in launchd
    assert "<false/>" in launchd
    home = Path("/Users/example")
    launchd = render_installed_unit(
        "launchd",
        repo=Path("/tmp/a&b"),
        python=Path("/usr/bin/python3"),
        env=env,
        home=home,
    )
    systemd = render_installed_unit(
        "systemd",
        repo=Path("/tmp/a&b"),
        python=Path("/opt/My Tools/python"),
        env=env,
        home=home,
    )
    plist = plistlib.loads(launchd.encode("utf-8"))
    launchd_path = service_path("launchd", home=home)
    systemd_path = service_path("systemd", home=home)
    assert plist["EnvironmentVariables"]["PATH"] == launchd_path
    assert launchd_path == (
        "/opt/homebrew/bin:/usr/local/bin:/Users/example/.grok/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    assert systemd_path == (
        "/opt/homebrew/bin:/usr/local/bin:/Users/example/.grok/bin:"
        "/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    assert f"Environment=PATH={systemd_path}" in systemd
    assert "$HOME" not in plist["EnvironmentVariables"]["PATH"]
    spaced = Path("/Users/My Home")
    quoted = render_installed_unit(
        "systemd",
        repo=Path("/tmp/a&b"),
        python=Path("/usr/bin/python3"),
        env=door_environment("openrouter"),
        home=spaced,
    )
    assert 'Environment="PATH=' in quoted
    assert "/Users/My Home/.grok/bin" in quoted
    assert service_path("systemd", home=spaced) in quoted
    assert 'Environment="OS3_BRIDGE_CODEX_BIN=/opt/My Tools/codex"' in systemd
    copilot_env = door_environment(
        "copilot",
        cli_path="/opt/My Tools/copilot",
    )
    assert copilot_env["OS3_BRIDGE_COPILOT_BIN"] == "/opt/My Tools/copilot"
    assert "OPENROUTER_API_KEY" not in copilot_env
    assert '"/opt/My Tools/python"' in systemd
    assert "os3_bridge.cli run" in systemd
    assert "Restart=on-failure" in systemd
    template = (repo_root() / "packaging" / "systemd" / "nexus.service").read_text(encoding="utf-8")
    rendered = render_unit(
        template,
        repo=repo_root(),
        python=Path("/usr/bin/python3"),
        env=door_environment("openrouter"),
        kind="systemd",
    )
    assert "OS3_BRIDGE_BACKEND=openrouter" in rendered


def test_launchd_and_systemd_commands_do_not_kill(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> CommandResult:
        calls.append(argv)
        return CommandResult(0, "\tpid = 77\n", "")

    launchd = LaunchdPlatform(tmp_path, run=run, sleep=lambda _s: None)
    assert launchd.packaged() == PackagedService(True, True, 77)
    launchd.install_and_start("<plist>ok</plist>\n")
    assert launchd.plist.read_text(encoding="utf-8").startswith("<plist>")
    assert launchd.plist.stat().st_mode & 0o777 == 0o644
    launchd.stop_and_remove()
    assert not launchd.plist.exists()
    assert calls[0][0] == "launchctl"
    assert calls[1][:2] == ["launchctl", "bootstrap"]
    assert calls[2][:2] == ["launchctl", "bootout"]
    assert all(argv[0] == "launchctl" for argv in calls)
    assert all("kill" not in argv for argv in calls)

    calls.clear()

    def show(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if "show" in argv:
            return CommandResult(
                0,
                "LoadState=not-found\nActiveState=inactive\nMainPID=0\n",
                "",
            )
        return CommandResult(1, "", "not loaded")

    systemd = SystemdPlatform(tmp_path, run=show)
    systemd.unit.parent.mkdir(parents=True)
    systemd.unit.write_text("old\n", encoding="utf-8")
    assert systemd.packaged().installed is True
    systemd.stop_and_remove()
    assert not systemd.unit.exists()
    assert calls[0][0] == "systemctl"
    assert "disable" in calls[1]
    assert "--now" in calls[1]
    assert calls[2][1:] == ["--user", "daemon-reload"]
    assert all(argv[0] == "systemctl" for argv in calls)
    assert all("kill" not in argv for argv in calls)

    shown = parse_systemctl_show("LoadState=loaded\nActiveState=active\nMainPID=15\n")
    assert shown == PackagedService(True, True, 15)
    idle = parse_systemctl_show("LoadState=loaded\nActiveState=inactive\nMainPID=0\n")
    assert idle == PackagedService(True, False, None)
    missing = parse_launchctl_print("", 1)
    assert missing == PackagedService(False, False, None)


def test_cli_start_stop_and_run_use_the_fake_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    busy = FakePlatform([Listener(9, "other")], PackagedService(False, False, None))
    _patch_start(monkeypatch, busy)
    assert main(["start"]) == 1
    assert busy.events == []
    err = capsys.readouterr().err
    assert "pid 9" in err
    assert "left running" in err.lower()

    free = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, free)
    assert main(["start"]) == 0
    assert free.events == ["start"]
    assert free.unit is not None
    assert "127.0.0.1" in free.unit
    assert "localhost" not in free.unit
    assert "<string>run</string>" in free.unit
    out = capsys.readouterr().out
    assert "Started packaged nexus" in out
    assert "sk-" not in out

    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    choice.write_text("codex\n", encoding="utf-8")
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(binary))
    coded = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, coded)
    assert main(["start"]) == 0
    assert coded.unit is not None
    assert str(binary) in coded.unit
    assert "sk-" not in coded.unit
    assert service_path("launchd") in coded.unit
    assert str(Path.home() / ".grok" / "bin") in coded.unit

    brew = tmp_path / "homebrew" / "bin"
    brew.mkdir(parents=True)
    brew_codex = brew / "codex"
    brew_codex.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    brew_codex.chmod(0o755)
    monkeypatch.delenv("OS3_BRIDGE_CODEX_BIN")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(
        "nexus.backends.cli_exec.BREW_BIN_PREFIXES",
        (str(brew), str(tmp_path / "local" / "bin")),
    )
    choice.write_text("openrouter\n", encoding="utf-8")
    brewed = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, brewed)
    assert main(["start"]) == 0
    assert brewed.unit is not None
    assert f"<string>{brew_codex}</string>" in brewed.unit
    assert "OS3_BRIDGE_CODEX_BIN" in brewed.unit
    assert service_path("launchd") in brewed.unit
    assert "/opt/homebrew/bin:/usr/local/bin" in brewed.unit
    assert str(Path.home() / ".grok" / "bin") in brewed.unit

    choice.write_text("codex\n", encoding="utf-8")
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(tmp_path / "missing-codex"))
    blocked = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, blocked)
    assert main(["start"]) == 1
    assert blocked.events == []

    choice.write_text("mlx\n", encoding="utf-8")
    unknown = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, unknown)
    assert main(["start"]) == 1
    assert "mlx" not in capsys.readouterr().err
    assert unknown.events == []

    choice.unlink()
    key = Path(os.environ["OS3_BRIDGE_OPENROUTER_KEY_FILE"])
    secret = "sk-or-status-secret-value"
    key.write_text(secret + "\n", encoding="utf-8")
    bridge = Path(os.environ["OS3_BRIDGE_KEY_FILE"])
    bridge.write_text("os3_status_bridge_secret\n", encoding="utf-8")
    quiet = FakePlatform([Listener(9, "other")], PackagedService(False, False, None))
    monkeypatch.setattr("nexus.cli.build_platform", lambda: quiet)
    assert main(["status"]) == 0
    status = capsys.readouterr().out
    assert secret not in status
    assert "os3_status_bridge_secret" not in status
    assert "present" in status
    assert "pid 9" in status

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("server was executed")

    monkeypatch.setattr(os, "execv", explode)
    monkeypatch.setattr(
        "nexus.cli.live_listeners",
        lambda _port=11435: [Listener(9, "other")],
    )
    assert main(["run"]) == 1
    monkeypatch.setenv("OS3_BRIDGE_SERVICE", "1")
    assert main(["run"]) == 0
    monkeypatch.delenv("OS3_BRIDGE_SERVICE")

    seen: dict[str, list[str]] = {}

    def fake_exec(_program: str, argv: list[str]) -> None:
        seen["argv"] = argv
        raise SystemExit(7)

    monkeypatch.setattr(os, "execv", fake_exec)
    monkeypatch.setattr("nexus.cli.live_listeners", lambda _port=11435: [])
    with pytest.raises(SystemExit) as exc:
        main(["run"])
    assert exc.value.code == 7
    assert seen["argv"][0] == sys.executable
    assert seen["argv"][1].endswith("server.py")

    def unavailable(_port: int = 11435) -> list[Listener]:
        raise PortCheckError(port_check_failure())

    monkeypatch.setattr(os, "execv", explode)
    monkeypatch.setattr("nexus.cli.live_listeners", unavailable)
    assert main(["run"]) == 1


def test_build_platform_matches_the_host() -> None:
    platform = build_platform()
    if sys.platform == "darwin":
        assert platform.kind == "launchd"
        assert platform.label == LAUNCHD_LABEL
    elif sys.platform.startswith("linux"):
        assert platform.kind == "systemd"
        assert platform.label == SYSTEMD_UNIT


def _proc_listener(proc: Path) -> None:
    net = proc / "net"
    net.mkdir(parents=True)
    header = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode\n"
    )
    established = (
        "   1: 0100007F:2CAB 0100007F:1F90 01 00000000:00000000 00:00000000 "
        "00000000 0 0 100 1 0000000000000000 100 0 0 10 0\n"
    )
    listen = (
        "   0: 0100007F:2CAB 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000 0 0 99 1 0000000000000000 100 0 0 10 0\n"
    )
    (net / "tcp").write_text(header + established + listen, encoding="utf-8")
    fd = proc / "42" / "fd"
    fd.mkdir(parents=True)
    (fd / "3").symlink_to("socket:[99]")
    (proc / "42" / "cmdline").write_bytes(b"/usr/bin/python\0server.py\0")


def _failing_systemctl(stderr: str, calls: list[list[str]]):
    def run(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "enable"]:
            return CommandResult(1, "", stderr)
        return CommandResult(0, "", "")

    return run


def test_failed_systemd_start_leaves_no_unit_and_says_what_to_do(tmp_path: Path) -> None:
    from nexus.service import ServiceError, SystemdPlatform

    calls: list[list[str]] = []
    platform = SystemdPlatform(
        tmp_path, run=_failing_systemctl("Failed to connect to bus: No medium found", calls)
    )
    with pytest.raises(ServiceError) as caught:
        platform.install_and_start("[Unit]\n")
    message = str(caught.value)
    assert "no systemd user session" in message
    assert "./nexus run" in message
    assert not platform.unit.exists()
    assert ["systemctl", "--user", "daemon-reload"] in calls[2:]


def test_missing_systemctl_is_a_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus import service
    from nexus.service import ServiceError, SystemdPlatform

    def gone(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    monkeypatch.setattr(service.subprocess, "run", gone)
    platform = SystemdPlatform(tmp_path)
    assert platform.packaged().installed is False
    with pytest.raises(ServiceError) as caught:
        platform.install_and_start("[Unit]\n")
    assert "systemctl is not available here" in str(caught.value)
    assert not platform.unit.exists()


def test_launchd_stop_waits_until_the_job_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_SERVICE_LABEL", "com.example.nexus-test")
    prints = iter(["\tpid = 4242\n", "\tpid = 4242\n", None])
    calls: list[list[str]] = []
    slept: list[float] = []

    def run(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[1] == "print":
            out = next(prints)
            return CommandResult(113, "", "not found") if out is None else CommandResult(0, out, "")
        return CommandResult(0, "", "")

    launchd = LaunchdPlatform(tmp_path, run=run, sleep=slept.append)
    launchd.plist.parent.mkdir(parents=True)
    launchd.plist.write_text("<plist/>", encoding="utf-8")
    launchd.stop_and_remove()
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert calls[0][2].endswith("/com.example.nexus-test")
    assert [c[1] for c in calls[1:]] == ["print", "print", "print"]
    assert len(slept) == 2
    assert not launchd.plist.exists()
    assert all("kill" not in argv for argv in calls)


def test_launchd_stop_wait_is_bounded(tmp_path: Path) -> None:
    def run(argv: list[str]) -> CommandResult:
        return CommandResult(0, "\tpid = 9\n", "")

    slept: list[float] = []
    LaunchdPlatform(tmp_path, run=run, sleep=slept.append).stop_and_remove()
    assert 0 < len(slept) <= 50
