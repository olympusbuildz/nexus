"""Naming the holder of the door port: parsers, classification, and the message."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from nexus import port_holder
from nexus.port_holder import classify, holder_lines, project_root, truncate_command
from nexus.service import (
    CommandResult,
    Listener,
    PackagedService,
    PortCheckError,
    busy_message,
    collect_listeners,
    execute_start,
    parse_ss_output,
)

ROOT = Path(__file__).resolve().parent.parent

SS_ONE = """\
State   Recv-Q  Send-Q   Local Address:Port   Peer Address:Port  Process
LISTEN  0       2048         127.0.0.1:11435       0.0.0.0:*      users:(("python3",pid=4242,fd=7))
"""
SS_HIDDEN = """\
State   Recv-Q  Send-Q   Local Address:Port   Peer Address:Port  Process
LISTEN  0       511          127.0.0.1:11435       0.0.0.0:*
"""
SS_TWO = (
    "LISTEN 0 128 127.0.0.1:11435 0.0.0.0:* "
    'users:(("node",pid=11,fd=3),("node",pid=12,fd=3))\n'
    'LISTEN 0 128 127.0.0.1:9999 0.0.0.0:* users:(("other",pid=13,fd=3))\n'
)


def _probe(command: str | None, cwd: str | None):
    def probe(pid: int) -> tuple[str | None, Path | None]:
        del pid
        return command, Path(cwd) if cwd else None

    return probe


def test_parse_ss_output_names_pid_and_ignores_other_ports() -> None:
    assert parse_ss_output(SS_ONE, 11435) == [Listener(4242, "python3")]
    assert parse_ss_output(SS_TWO, 11435) == [Listener(11, "node"), Listener(12, "node")]
    assert parse_ss_output("", 11435) == []


def test_ss_row_without_users_is_an_unnamed_listener() -> None:
    assert parse_ss_output(SS_HIDDEN, 11435) == [Listener(0, "unknown")]


def test_collect_listeners_uses_ss_when_lsof_is_absent(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> CommandResult:
        calls.append(argv)
        return CommandResult(0, SS_ONE, "")

    found = collect_listeners(
        11435,
        run=run,
        which=lambda name: "/usr/bin/ss" if name == "ss" else None,
        proc_root=tmp_path / "proc",
    )
    assert found == [Listener(4242, "python3")]
    assert calls[0][0] == "ss"
    failing = lambda _argv: CommandResult(1, "", "ss: boom")  # noqa: E731
    with pytest.raises(PortCheckError):
        collect_listeners(
            11435,
            run=failing,
            which=lambda name: "/usr/bin/ss" if name == "ss" else None,
            proc_root=tmp_path / "proc",
        )


def test_neither_tool_nor_proc_degrades_to_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(PortCheckError) as caught:
        collect_listeners(
            11435,
            run=lambda _argv: CommandResult(127, "", ""),
            which=lambda _name: None,
            proc_root=tmp_path / "no-proc",
        )
    assert "left unchanged" in str(caught.value)


def test_service_pid_is_named_as_our_service() -> None:
    held = classify(
        77, "python", service_pid=77, probe=_probe("/x/.venv/bin/python server.py", None)
    )
    assert held.kind == "service"
    assert "ai.nexus.server" in held.summary or "nexus.service" in held.summary
    assert "./nexus stop" in held.next_step
    if sys.platform == "darwin":
        assert "launchctl print gui/$UID/ai.nexus.server" in held.next_step


def test_this_checkout_and_other_checkout_are_told_apart(tmp_path: Path) -> None:
    here = tmp_path / "here"
    older = tmp_path / "older"
    for checkout in (here, older):
        (checkout / "os3_bridge").mkdir(parents=True)
    mine = classify(
        5,
        "python",
        service_pid=None,
        probe=_probe(f"{here}/.venv/bin/python {here}/server.py", None),
        repo=here,
    )
    assert mine.kind == "this_checkout"
    assert "Ctrl-C" in mine.next_step
    other = classify(
        6,
        "python",
        service_pid=None,
        probe=_probe(f"{older}/.venv/bin/python -m os3_bridge.cli run", str(older)),
        repo=here,
    )
    assert other.kind == "other_checkout"
    assert str(older) in other.summary
    assert f"cd {older} && ./nexus stop" in other.next_step
    unknown = classify(
        8, "python", service_pid=None, probe=_probe("python -m os3_bridge.cli run", None), repo=here
    )
    assert unknown.kind == "os3_unknown"


def test_a_stranger_is_not_ours_and_is_never_told_to_be_killed() -> None:
    held = classify(9, "node", service_pid=1, probe=_probe("node /srv/app.js --port 11435", "/srv"))
    assert held.kind == "other"
    assert held.summary == "not nexus"
    assert "kill" not in held.next_step.lower()
    assert "Nothing was signaled" in held.next_step


def test_unreadable_holder_still_gets_a_next_step() -> None:
    held = classify(0, "unknown", service_pid=None, probe=_probe(None, None))
    assert held.kind == "other"
    assert "lsof -nP -iTCP:11435" in held.next_step


def test_project_root_ignores_a_server_py_that_is_not_ours(tmp_path: Path) -> None:
    assert project_root(f"python {tmp_path}/server.py", None) == (False, None)
    assert project_root("python -m http.server", tmp_path) == (False, None)


def test_command_is_truncated_and_credential_arguments_masked() -> None:
    long = "python " + "a" * 300
    shown = truncate_command(long)
    assert len(shown) == 100
    assert shown.endswith("...")
    masked = truncate_command("tool --api-key sk-live-123456789 --token=abcdefgh run")
    assert "sk-live" not in masked
    assert "abcdefgh" not in masked


def test_busy_message_names_the_holder_and_keeps_the_old_promises() -> None:
    message = busy_message([Listener(9, "other")], service_pid=None)
    assert "127.0.0.1:11435 is already in use:" in message
    assert "pid 9" in message
    assert "held by:" in message
    assert "next:" in message
    assert "left running" in message.lower()
    assert "Nothing was stopped or signaled" in message


def test_execute_start_passes_the_service_pid_so_our_own_unit_is_named() -> None:
    class Platform:
        def listeners(self) -> list[Listener]:
            return [Listener(9, "other"), Listener(31, "python")]

        def packaged(self) -> PackagedService:
            return PackagedService(True, True, 31)

    outcome = execute_start(Platform(), "unit")
    assert outcome.code == 1
    assert "pid 9" in outcome.message
    assert "pid 31" in outcome.message
    assert "our own packaged service" in outcome.message
    assert "also loaded" in outcome.message


def test_holder_lines_for_a_real_listener_name_this_process() -> None:
    lines = holder_lines([Listener(os.getpid(), "python")], service_pid=None)
    assert lines[0].startswith(f"  pid {os.getpid()} ")
    assert any("held by:" in line for line in lines)


def test_doctor_listen_section_names_a_real_listener(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    port = probe.getsockname()[1]
    try:
        done = subprocess.run(
            [sys.executable, "-m", "nexus.port_holder", str(port)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
    finally:
        probe.close()
    assert done.returncode == 0, done.stderr
    if "could not check" in done.stdout.lower():
        pytest.skip("neither lsof, ss nor /proc is usable here")
    assert f"pid {os.getpid()}" in done.stdout
    assert "held by:" in done.stdout
    assert "process left running" in done.stdout
    free = subprocess.run(
        [sys.executable, "-m", "nexus.port_holder", str(port)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert f"nothing listening on 127.0.0.1:{port}" in free.stdout


def test_port_holder_module_never_signals() -> None:
    text = Path(port_holder.__file__).read_text(encoding="utf-8")
    for banned in ("os.kill", "signal.", "killpg", "terminate(", ".kill("):
        assert banned not in text
