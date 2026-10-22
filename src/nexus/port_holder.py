"""Name whatever holds the door port, and say the safe next step.

Read-only. Nothing in this module signals, stops, or restarts a process: a
holder we did not start is never ours to kill, and the next step is always a
command the operator runs. (``./nexus stop`` in service.py signals only a holder
this module classifies as a foreground nexus from this checkout.)
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nexus.config import DEFAULT_HOST, configured_port, repo_root
from nexus.redact import redact
from nexus.service import launchd_label, run_command, systemd_unit_name

COMMAND_LIMIT = 100

_SECRET_FLAG = re.compile(r"(?i)(--?[\w-]*(?:key|token|secret|password)[\w-]*)([= ])\S+")
# os3_bridge.cli is the LaunchAgent command. nexus.cli and the ./nexus wrapper
# are the same process.
_PROJECT_TOKEN = re.compile(r"os3[_-]bridge|^nexus(?:\.cli)?$")

Probe = Callable[[int], tuple[str | None, Path | None]]


@dataclass(frozen=True)
class Holder:
    pid: int
    command: str
    kind: Literal["service", "this_checkout", "other_checkout", "os3_unknown", "other"]
    summary: str
    next_step: str


def _run(argv: list[str]) -> str | None:
    done = run_command(argv)
    return done.stdout if done.returncode == 0 else None


def inspect_process(pid: int) -> tuple[str | None, Path | None]:
    """Full command line and working directory of ``pid``, as far as the OS allows."""
    command: str | None = None
    cwd: Path | None = None
    proc = Path("/proc") / str(pid)
    try:
        raw = (proc / "cmdline").read_bytes()
        command = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip() or None
    except OSError:
        pass
    try:
        cwd = Path(os.readlink(proc / "cwd"))
    except OSError:
        pass
    if command is None and shutil.which("ps"):
        text = _run(["ps", "-p", str(pid), "-o", "command="])
        command = text.strip() if text and text.strip() else None
    if cwd is None and shutil.which("lsof"):
        text = _run(["lsof", "-nP", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        for line in (text or "").splitlines():
            if line.startswith("n") and len(line) > 1:
                cwd = Path(line[1:])
    return command, cwd


def truncate_command(command: str, limit: int = COMMAND_LIMIT) -> str:
    """One printable line, with credential-shaped arguments masked."""
    text = redact(" ".join(command.split()))
    text = _SECRET_FLAG.sub(r"\1\2[REDACTED]", text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def project_root(command: str, cwd: Path | None) -> tuple[bool, Path | None]:
    """Whether ``command`` looks like Nexus, and the checkout it runs from."""
    tokens = command.split()
    venv_root = next(
        (Path(t.split("/.venv/", 1)[0]) for t in tokens if t.startswith("/") and "/.venv/" in t),
        None,
    )
    for token in tokens:
        name = Path(token).name
        absolute = Path(token).is_absolute()
        if name in {"server.py", "os3-bridge", "nexus"}:
            base = Path(token).parent if absolute else cwd
            if name == "server.py" and not _is_checkout(base):
                continue
            return True, base
        if _PROJECT_TOKEN.search(token) and not absolute:
            return True, venv_root or cwd
    return False, None


def _is_checkout(base: Path | None) -> bool:
    if base is None:
        return False
    return (base / "os3_bridge").is_dir() or (base / "src" / "nexus").is_dir()


def _same_dir(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _service_words() -> tuple[str, str]:
    if sys.platform == "darwin":
        return (
            f"LaunchAgent {launchd_label()}",
            f"launchctl print gui/$UID/{launchd_label()}",
        )
    unit = systemd_unit_name()
    return (f"systemd user unit {unit}", f"systemctl --user status {unit}")


def classify(
    pid: int,
    command: str,
    *,
    service_pid: int | None,
    probe: Probe = inspect_process,
    repo: Path | None = None,
) -> Holder:
    """Say who ``pid`` is. ``command`` is what the port scan saw; the probe may know more."""
    found_command, cwd = probe(pid) if pid > 0 else (None, None)
    full = found_command or command
    shown = truncate_command(full)
    here = repo if repo is not None else repo_root()
    if pid > 0 and service_pid is not None and pid == service_pid:
        label, inspect = _service_words()
        return Holder(
            pid,
            shown,
            "service",
            f"our own packaged service ({label})",
            f"./nexus stop unloads it (./nexus start reloads it). Inspect: {inspect}",
        )
    if pid <= 0:
        return Holder(
            pid,
            shown,
            "other",
            "a process this user cannot inspect (the OS hides its pid)",
            f"Run as its owner: lsof -nP -iTCP:{configured_port()} -sTCP:LISTEN ."
            " Nothing was signaled.",
        )
    ours, root = project_root(full, cwd)
    if ours and root is not None and _same_dir(root, here):
        return Holder(
            pid,
            shown,
            "this_checkout",
            "a foreground nexus from this checkout (not the packaged service)",
            "./nexus stop ends it, or press Ctrl-C in the terminal running it.",
        )
    if ours and root is not None:
        return Holder(
            pid,
            shown,
            "other_checkout",
            f"another checkout of nexus ({root})",
            "Press Ctrl-C in the terminal running it, or if it was started as a service: "
            f"cd {shlex.quote(str(root))} && ./nexus stop",
        )
    if ours:
        return Holder(
            pid,
            shown,
            "os3_unknown",
            "a nexus process (its checkout could not be read)",
            "Press Ctrl-C in the terminal running it, or ./nexus stop if it is the service.",
        )
    return Holder(
        pid,
        shown,
        "other",
        "not nexus",
        "Close that program (Ctrl-C in its terminal, or quit the app), then run "
        "./nexus start. Nothing was signaled.",
    )


def holder_lines(
    listeners: list,
    *,
    service_pid: int | None = None,
    probe: Probe = inspect_process,
    repo: Path | None = None,
) -> list[str]:
    lines: list[str] = []
    for item in listeners:
        held = classify(
            item.pid, item.command or "unknown", service_pid=service_pid, probe=probe, repo=repo
        )
        who = f"pid {held.pid}" if held.pid > 0 else "pid unknown"
        lines.append(f"  {who} {held.command or 'unknown'}")
        lines.append(f"    held by: {held.summary}")
        lines.append(f"    next: {held.next_step}")
    return lines


def current_service_pid() -> int | None:
    from nexus.service import ServiceError, build_platform

    try:
        return build_platform().packaged().pid
    except (ServiceError, OSError):
        return None


def report(port: int | None = None) -> list[str]:
    """doctor.sh's listen section. Never raises."""
    port = configured_port() if port is None else port
    from nexus.service import ServiceError, live_listeners

    try:
        listeners = live_listeners(port)
    except ServiceError as exc:
        return [str(exc)]
    if not listeners:
        return [f"nothing listening on {DEFAULT_HOST}:{port}"]
    return [f"LISTEN {DEFAULT_HOST}:{port} (process left running):"] + holder_lines(
        listeners, service_pid=current_service_pid()
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    port = int(args[0]) if args and args[0].isdigit() else configured_port()
    print("\n".join(report(port)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
