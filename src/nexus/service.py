"""Packaged service install. Never starts a second listener on the door port.

macOS uses a LaunchAgent. Linux uses a systemd user unit. Both run
``nexus run`` (the installed unit still invokes ``os3_bridge.cli run``, which
is the same command). The process exits before binding when the port is taken.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from nexus.backends.cli_exec import grok_bin_dir
from nexus.config import (
    ANTIGRAVITY_ENABLE_ENV,
    BACKEND_IDS,
    CLI_BIN_ENV,
    DEFAULT_HOST,
    antigravity_enabled,
    canonical_env,
    configured_port,
    environ_get,
    export_setting,
    repo_root,
)

LAUNCHD_LABEL = "ai.nexus.server"
LEGACY_LAUNCHD_LABEL = "com.grokbot.os3-bridge"
LABEL_ENV = "NEXUS_SERVICE_LABEL"
_LABEL_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SYSTEMD_UNIT = "nexus.service"
LEGACY_SYSTEMD_UNIT = "os3-bridge.service"
LEGACY_PIN_FILE = ".nexus_legacy_service"
_SYSTEMD_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
# Setting PATH in a LaunchAgent or systemd user unit replaces the default.
# Homebrew, then the absolute Grok Build dir, then the platform's system PATH.
_LAUNCHD_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_SYSTEMD_SYSTEM_PATH = "/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
_PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin"
_COMMAND_TIMEOUT = 10.0
_log = logging.getLogger("nexus.service")
_warned_both: set[str] = set()


class ServiceError(Exception):
    """User-facing service failure. The message must not contain a secret."""


class PortCheckError(ServiceError):
    """The door port could not be inspected. Callers must not bind or stop."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Listener:
    pid: int
    command: str


@dataclass(frozen=True)
class PackagedService:
    installed: bool
    running: bool
    pid: int | None


@dataclass(frozen=True)
class Outcome:
    code: int
    message: str


def _subprocess_run(argv: list[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=_COMMAND_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]}: timed out after {_COMMAND_TIMEOUT:g}s")
    except OSError as exc:
        return CommandResult(127, "", f"{argv[0]}: {exc.strerror or 'could not be run'}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


run_command = _subprocess_run


def launchd_agent_path(label: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def systemd_unit_path(unit: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "systemd" / "user" / unit


def legacy_pin_path() -> Path:
    return repo_root() / LEGACY_PIN_FILE


def legacy_pinned() -> bool:
    return legacy_pin_path().is_file()


def pin_legacy_service() -> bool:
    """Remember that this checkout runs the legacy-named service.

    ``stop`` removes the plist or unit, and the menu bar's Restart runs stop then start.
    Without this marker that restart would quietly install the service under the new name.
    """
    try:
        legacy_pin_path().write_text(
            "This checkout's packaged service uses its pre-Nexus name.\n"
            "Delete this file after ./nexus stop to let the next ./nexus start use the new name.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning("could not record the legacy service name in %s: %s", legacy_pin_path(), exc)
        return False
    return True


def _resolve_service_name(new: str, legacy: str, new_file: Path, legacy_file: Path) -> str:
    """New name unless only the legacy one is installed (or was, before a ``stop``)."""
    new_there, legacy_there = new_file.is_file(), legacy_file.is_file()
    if new_there and legacy_there:
        if new not in _warned_both:
            _warned_both.add(new)
            _log.warning(
                "both %s and the legacy %s are installed; managing %s. The legacy one was "
                "left alone. Stop it by hand and remove %s.",
                new,
                legacy,
                new,
                legacy_file,
            )
        return new
    if new_there:
        return new
    if legacy_there or legacy_pinned():
        return legacy
    return new


def launchd_label(home: Path | None = None) -> str:
    """The LaunchAgent label. ``NEXUS_SERVICE_LABEL`` overrides it, for a second checkout.

    Otherwise ``ai.nexus.server``, except where only the legacy
    ``com.grokbot.os3-bridge`` agent is installed: that machine keeps its label.
    """
    raw = (environ_get(LABEL_ENV) or "").strip()
    if raw:
        if not _LABEL_SAFE.fullmatch(raw):
            raise ServiceError(f"{LABEL_ENV} must be letters, digits, '.', '-' or '_': {raw!r}")
        return raw
    return _resolve_service_name(
        LAUNCHD_LABEL,
        LEGACY_LAUNCHD_LABEL,
        launchd_agent_path(LAUNCHD_LABEL, home),
        launchd_agent_path(LEGACY_LAUNCHD_LABEL, home),
    )


def systemd_unit_name(home: Path | None = None) -> str:
    """``nexus.service``, or the legacy ``os3-bridge.service`` where only that is installed."""
    return _resolve_service_name(
        SYSTEMD_UNIT,
        LEGACY_SYSTEMD_UNIT,
        systemd_unit_path(SYSTEMD_UNIT, home),
        systemd_unit_path(LEGACY_SYSTEMD_UNIT, home),
    )


def port_check_failure() -> str:
    return (
        f"Could not check whether {DEFAULT_HOST}:{configured_port()} is in use. "
        "The packaged service was left unchanged."
    )


def parse_lsof_field_output(text: str) -> list[Listener]:
    listeners: list[Listener] = []
    seen: set[int] = set()
    pid: int | None = None
    command = ""

    def flush() -> None:
        nonlocal pid, command
        if pid is None or pid in seen:
            pid = None
            command = ""
            return
        seen.add(pid)
        listeners.append(Listener(pid=pid, command=command or "unknown"))
        pid = None
        command = ""

    for line in text.splitlines():
        if not line:
            continue
        kind, value = line[0], line[1:]
        if kind == "p":
            flush()
            pid = int(value) if value.isdigit() else None
            command = ""
        elif kind == "c" and pid is not None and value.strip():
            command = value.strip()
    flush()
    return listeners


_SS_USERS = re.compile(r'\("([^"]*)",pid=(\d+)')


def parse_ss_output(text: str, port: int) -> list[Listener]:
    """Listeners from ``ss -ltnp``. A row with no ``users:`` column is a listener we cannot name."""
    listeners: list[Listener] = []
    seen: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        if not parts[3].endswith(f":{port}"):
            continue
        named = _SS_USERS.findall(line)
        if not named:
            if 0 not in seen:
                seen.add(0)
                listeners.append(Listener(pid=0, command="unknown"))
            continue
        for name, raw in named:
            pid = int(raw)
            if pid not in seen:
                seen.add(pid)
                listeners.append(Listener(pid=pid, command=name or "unknown"))
    return listeners


def _listen_inodes(text: str, port: int) -> set[str]:
    inodes: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] == "sl":
            continue
        local = parts[1]
        if ":" not in local:
            continue
        try:
            found_port = int(local.rsplit(":", 1)[1], 16)
        except ValueError:
            continue
        if found_port == port and parts[3] == "0A":
            inodes.add(parts[9])
    return inodes


def _cmdline(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return "unknown"
    text = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    return text or "unknown"


def listeners_from_proc(proc_root: Path, port: int) -> list[Listener]:
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        table = proc_root / "net" / name
        if table.is_file():
            try:
                inodes |= _listen_inodes(table.read_text(encoding="utf-8", errors="replace"), port)
            except OSError as exc:
                raise PortCheckError(port_check_failure()) from exc
    if not inodes:
        return []
    found: list[Listener] = []
    seen: set[int] = set()
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise PortCheckError(port_check_failure()) from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        if not fd_dir.is_dir():
            continue
        matched = False
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if any(target == f"socket:[{inode}]" for inode in inodes):
                matched = True
                break
        if not matched:
            continue
        pid = int(entry.name)
        if pid in seen:
            continue
        seen.add(pid)
        found.append(Listener(pid=pid, command=_cmdline(entry / "cmdline")))
    if not found:
        return [Listener(pid=0, command="unknown")]
    return found


def collect_listeners(
    port: int,
    *,
    run: Callable[[list[str]], CommandResult],
    which: Callable[[str], str | None],
    proc_root: Path,
) -> list[Listener]:
    if which("lsof"):
        result = run(["lsof", "-nP", "-Fpc", f"-iTCP:{port}", "-sTCP:LISTEN"])
        if result.returncode in {0, 1}:
            return parse_lsof_field_output(result.stdout)
        raise PortCheckError(port_check_failure())
    if which("ss"):
        result = run(["ss", "-ltnp", "sport", "=", f":{port}"])
        if result.returncode == 0:
            return parse_ss_output(result.stdout, port)
        raise PortCheckError(port_check_failure())
    if (proc_root / "net" / "tcp").is_file():
        return listeners_from_proc(proc_root, port)
    raise PortCheckError(port_check_failure())


def live_listeners(port: int | None = None) -> list[Listener]:
    return collect_listeners(
        configured_port() if port is None else port,
        run=_subprocess_run,
        which=shutil.which,
        proc_root=Path("/proc"),
    )


def parse_launchctl_print(stdout: str, returncode: int) -> PackagedService:
    if returncode != 0:
        return PackagedService(installed=False, running=False, pid=None)
    pid: int | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("pid = "):
            continue
        raw = stripped.split("=", 1)[1].strip()
        if raw.isdigit() and int(raw) > 0:
            pid = int(raw)
    return PackagedService(installed=True, running=pid is not None, pid=pid)


def parse_systemctl_show(text: str) -> PackagedService:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip()
    load = values.get("LoadState", "not-found")
    active = values.get("ActiveState", "inactive")
    raw_pid = values.get("MainPID", "0")
    pid = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else None
    installed = load not in {"not-found", "masked", ""}
    running = active == "active" or pid is not None
    return PackagedService(installed=installed, running=running, pid=pid)


def busy_message(
    listeners: list[Listener], *, service_pid: int | None = None, stopping: bool = False
) -> str:
    from nexus.port_holder import holder_lines

    lines = [f"{DEFAULT_HOST}:{configured_port()} is already in use:"]
    lines.extend(holder_lines(listeners, service_pid=service_pid))
    if stopping:
        lines.append("That process was left running. Nothing was signaled.")
    else:
        lines.append("Refusing to start a second listener.")
        lines.append("That process was left running. Nothing was stopped or signaled.")
    return "\n".join(lines)


def _already_message(pid: int) -> str:
    return (
        f"Packaged nexus is already listening on {DEFAULT_HOST}:{configured_port()} "
        f"(pid {pid}).\n"
        "Left it running.\n"
        "./nexus stop then ./nexus start reloads the service.\n"
        "Check it with ./doctor.sh"
    )


def execute_start(platform: object, unit_text: str) -> Outcome:
    try:
        listeners = platform.listeners()  # type: ignore[attr-defined]
    except PortCheckError as exc:
        return Outcome(1, str(exc))
    service = platform.packaged()  # type: ignore[attr-defined]
    ours = {service.pid} if service.pid else set()
    foreign = [item for item in listeners if item.pid not in ours]
    if listeners and not foreign:
        pid = service.pid if service.pid is not None else listeners[0].pid
        return Outcome(0, _already_message(pid))
    if foreign:
        message = busy_message(listeners, service_pid=service.pid)
        if service.installed:
            message += (
                "\nThe packaged service is also loaded. ./nexus stop unloads that service only."
            )
        return Outcome(1, message)
    replaced = service.installed or service.running
    if replaced:
        platform.stop_and_remove()  # type: ignore[attr-defined]
    platform.install_and_start(unit_text)  # type: ignore[attr-defined]
    message = (
        f"Started packaged nexus on http://{DEFAULT_HOST}:{configured_port()}/v1\n"
        "Check it with ./doctor.sh"
    )
    if replaced:
        message = "Replaced the previous packaged service.\n" + message
    return Outcome(0, message)


def _stop_foreground(
    platform: object,
    listeners: list[Listener],
    *,
    service_pid: int | None,
    probe: Callable[[int], tuple[str | None, Path | None]],
    terminate: Callable[[int, int], None],
    sleep: Callable[[float], None],
) -> tuple[list[Listener], list[str], bool]:
    """SIGTERM a foreground nexus that runs from this checkout. Nothing else is ever signaled."""
    from nexus.port_holder import classify

    mine = [
        item
        for item in listeners
        if item.pid > 0
        and item.pid != service_pid
        and classify(item.pid, item.command or "unknown", service_pid=service_pid, probe=probe).kind
        == "this_checkout"
    ]
    if not mine:
        return listeners, [], False
    pids = {item.pid for item in mine}
    for pid in sorted(pids):
        try:
            terminate(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return listeners, [f"Could not signal the foreground nexus (pid {pid}): {exc}"], True
    for _ in range(50):
        if not pids & {item.pid for item in platform.listeners()}:  # type: ignore[attr-defined]
            break
        sleep(0.1)
    alive = pids & {item.pid for item in platform.listeners()}  # type: ignore[attr-defined]
    lines = [
        f"Stopped the foreground nexus (pid {pid})."
        if pid not in alive
        else f"The foreground nexus (pid {pid}) did not exit after SIGTERM and is still running."
        for pid in sorted(pids)
    ]
    others = [item for item in listeners if item.pid not in pids]
    return others, lines, bool(alive)


def execute_stop(
    platform: object,
    *,
    probe: Callable[[int], tuple[str | None, Path | None]] | None = None,
    terminate: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    from nexus.port_holder import inspect_process

    try:
        listeners = platform.listeners()  # type: ignore[attr-defined]
    except PortCheckError as exc:
        return Outcome(1, str(exc))
    service = platform.packaged()  # type: ignore[attr-defined]
    listeners, foreground, failed = _stop_foreground(
        platform,
        listeners,
        service_pid=service.pid,
        probe=probe or inspect_process,
        terminate=terminate,
        sleep=sleep,
    )
    if not service.installed and not service.running:
        head = "\n".join(foreground) or "Packaged nexus is not installed."
        if listeners:
            return Outcome(1, head + "\n" + busy_message(listeners, stopping=True))
        return Outcome(1 if failed else 0, head)
    platform.stop_and_remove()  # type: ignore[attr-defined]
    lines = [
        *foreground,
        "Stopped packaged nexus.",
        "Key files were kept.",
        "Other processes were not signaled.",
    ]
    if getattr(platform, "pin_failed", False):
        old = platform.label  # type: ignore[attr-defined]
        new = LAUNCHD_LABEL if platform.kind == "launchd" else SYSTEMD_UNIT  # type: ignore[attr-defined]
        lines.append(
            f"Warning: could not write {legacy_pin_path()}, so the next ./nexus start installs "
            f"{new} instead of {old}. Create that file (any content) to keep {old}."
        )
    ours = {service.pid} if service.pid else set()
    remaining = [item for item in listeners if item.pid not in ours]
    if remaining:
        lines.append(busy_message(remaining, service_pid=service.pid, stopping=True))
    return Outcome(1 if failed else 0, "\n".join(lines))


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _systemd_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ServiceError("refusing a multi-line service path")
    if _SYSTEMD_SAFE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def env_xml(env: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key, value in env.items():
        lines.append(f"    <key>{_xml(key)}</key>")
        lines.append(f"    <string>{_xml(value)}</string>")
    return "\n".join(lines)


def _systemd_env(key: str, value: str) -> str:
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise ServiceError("refusing an unexpected environment name")
    if "\n" in value or "\r" in value:
        raise ServiceError("refusing a multi-line environment value")
    if _SYSTEMD_SAFE.fullmatch(value):
        return f"Environment={key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{key}={escaped}"'


def env_lines(env: Mapping[str, str]) -> str:
    return "\n".join(_systemd_env(key, value) for key, value in env.items())


def _known_cli_bin(key: str) -> bool:
    names = set(CLI_BIN_ENV.values())
    if key in names:
        return True
    primary = canonical_env(key)
    return primary in names if primary else False


def _assign_cli_bin(env: dict[str, str], key: str, value: str) -> None:
    if not _known_cli_bin(key):
        raise ServiceError("refusing an unexpected CLI path variable")
    if "\n" in value or "\r" in value:
        raise ServiceError("The CLI path must be a single line.")
    export_setting(env, key, value)


def door_environment(
    backend: str,
    *,
    model: str | None = None,
    cli_path: str | None = None,
    cli_bins: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if backend not in BACKEND_IDS:
        raise ServiceError("The backend choice file is not a known backend. Run ./nexus setup.")
    env: dict[str, str] = {}
    export_setting(env, "NEXUS_HOST", DEFAULT_HOST)
    export_setting(env, "NEXUS_PORT", str(configured_port()))
    export_setting(env, "NEXUS_BACKEND", backend)
    export_setting(env, "NEXUS_SERVICE", "1")
    if (environ_get(LABEL_ENV) or "").strip():
        env[LABEL_ENV] = launchd_label()
    for suffix in ("RABBIT_WATCHDOG", "RABBIT_AGENT_LABEL"):
        value = (environ_get("NEXUS_" + suffix) or "").strip()
        if value:
            export_setting(env, "NEXUS_" + suffix, value)
    if model:
        if any(char.isspace() for char in model):
            raise ServiceError("NEXUS_MODEL must be a single token.")
        export_setting(env, "NEXUS_MODEL", model)
    if antigravity_enabled():
        export_setting(env, ANTIGRAVITY_ENABLE_ENV, "1")
    if cli_bins:
        for key, value in cli_bins.items():
            _assign_cli_bin(env, key, value)
    if cli_path:
        if "\n" in cli_path or "\r" in cli_path:
            raise ServiceError("The CLI path must be a single line.")
        bin_env = CLI_BIN_ENV.get(backend)
        if bin_env:
            export_setting(env, bin_env, cli_path)
    return env


def service_path(kind: str, *, home: Path | None = None) -> str:
    """PATH for a packaged unit. ``$HOME/.grok/bin`` is an absolute directory."""
    if home is None:
        grok = grok_bin_dir()
    else:
        root = home.expanduser()
        if not root.is_absolute():
            raise ServiceError("service home must be absolute")
        grok = root / ".grok" / "bin"
    grok_text = str(grok)
    if any(char in grok_text for char in (":", "\n", "\r")):
        raise ServiceError("refusing a service home that cannot sit on PATH")
    head = f"{_PATH_PREFIX}:{grok_text}"
    if kind == "launchd":
        return f"{head}:{_LAUNCHD_SYSTEM_PATH}"
    if kind == "systemd":
        return f"{head}:{_SYSTEMD_SYSTEM_PATH}"
    raise ServiceError(f"unknown service kind {kind}")


def render_unit(
    template: str,
    *,
    repo: Path,
    python: Path,
    env: Mapping[str, str],
    kind: str,
    home: Path | None = None,
) -> str:
    host = env.get("NEXUS_HOST", env.get("OS3_BRIDGE_HOST"))
    port = env.get("NEXUS_PORT", env.get("OS3_BRIDGE_PORT"))
    if host != DEFAULT_HOST or port != str(configured_port()):
        raise ServiceError(f"the packaged service binds 127.0.0.1:{configured_port()}")
    path_value = service_path(kind, home=home)
    if kind == "launchd":
        mapping = {
            "__ENV_XML__": env_xml(env),
            "__PYTHON__": _xml(str(python)),
            "__REPO__": _xml(str(repo)),
            "__SERVICE_PATH__": _xml(path_value),
            f"<string>{LAUNCHD_LABEL}</string>": f"<string>{_xml(launchd_label(home))}</string>",
        }
    elif kind == "systemd":
        template = template.replace(
            "Environment=PATH=__SERVICE_PATH__",
            _systemd_env("PATH", path_value),
        )
        mapping = {
            "__ENV_LINES__": env_lines(env),
            "__PYTHON__": _systemd_quote(str(python)),
            "__REPO__": _systemd_quote(str(repo)),
        }
    else:
        raise ServiceError(f"unknown service kind {kind}")
    text = template
    for token in sorted(mapping, key=len, reverse=True):
        text = text.replace(token, mapping[token])
    for token in ("__REPO__", "__PYTHON__", "__ENV_XML__", "__ENV_LINES__", "__SERVICE_PATH__"):
        if token in text:
            raise ServiceError(f"unit template still contains {token}")
    return text


def render_installed_unit(
    kind: str,
    *,
    repo: Path,
    python: Path,
    env: Mapping[str, str],
    home: Path | None = None,
) -> str:
    if kind == "launchd":
        path = repo_root() / "packaging" / "launchd" / f"{LAUNCHD_LABEL}.plist.template"
    elif kind == "systemd":
        path = repo_root() / "packaging" / "systemd" / SYSTEMD_UNIT
    else:
        raise ServiceError(f"unknown service kind {kind}")
    template = path.read_text(encoding="utf-8")
    return render_unit(template, repo=repo, python=python, env=env, kind=kind, home=home)


def venv_python(root: Path | None = None) -> Path:
    path = (root or repo_root()) / ".venv" / "bin" / "python"
    if not os.access(path, os.X_OK):
        raise ServiceError(f"No virtualenv at {path}. Run ./install.sh")
    return path


def _service_failure(tool: str, detail: str) -> str:
    """Say what a failed service command means and what to run instead."""
    lowered = detail.lower()
    if "bus" in lowered or "not found" in lowered or "no such file" in lowered:
        reason = (
            f"{tool} is not available here"
            if "not found" in lowered or "no such file" in lowered
            else "there is no systemd user session here (common over plain SSH, in "
            "containers, and on minimal servers)"
        )
        return (
            f"Could not install the packaged service: {reason}.\n"
            "Run the door in the foreground instead: ./nexus run\n"
            "Nothing was left installed."
        )
    return f"{detail or tool + ' failed'}\nNothing was left installed."


class LaunchdPlatform:
    kind = "launchd"

    def __init__(
        self,
        home: Path,
        run: Callable[[list[str]], CommandResult] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.home = home
        self.label = launchd_label(home)
        self._sleep = sleep or time.sleep
        self.plist = launchd_agent_path(self.label, home)
        self.pin_failed = False
        self._run = run or _subprocess_run

    def listeners(self) -> list[Listener]:
        return live_listeners()

    def packaged(self) -> PackagedService:
        result = self._run(["launchctl", "print", f"gui/{os.getuid()}/{self.label}"])
        shown = parse_launchctl_print(result.stdout, result.returncode)
        if self.plist.is_file() and not shown.installed:
            return PackagedService(installed=True, running=False, pid=None)
        return shown

    def install_and_start(self, unit_text: str) -> None:
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text(unit_text, encoding="utf-8")
        os.chmod(self.plist, 0o644)
        result = self._run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist)])
        if result.returncode != 0:
            self.plist.unlink(missing_ok=True)
            detail = (result.stderr or result.stdout).strip()
            raise ServiceError(
                _service_failure("launchctl", detail or "launchctl bootstrap failed")
            )

    def stop_and_remove(self) -> None:
        if self.label == LEGACY_LAUNCHD_LABEL:
            self.pin_failed = not pin_legacy_service()
        self._run(["launchctl", "bootout", f"gui/{os.getuid()}/{self.label}"])
        if self.plist.is_file():
            self.plist.unlink()
        # bootout returns before launchd has reaped the job. Wait (bounded) until
        # this label's job is gone, so `stop` does not return with the port still held.
        for _ in range(_STOP_POLLS):
            shown = self._run(["launchctl", "print", f"gui/{os.getuid()}/{self.label}"])
            if shown.returncode != 0 or parse_launchctl_print(shown.stdout, 0).pid is None:
                return
            self._sleep(_STOP_POLL_S)


_STOP_POLLS = 50
_STOP_POLL_S = 0.2


class SystemdPlatform:
    kind = "systemd"

    def __init__(
        self,
        home: Path,
        run: Callable[[list[str]], CommandResult] | None = None,
    ) -> None:
        self.home = home
        self.label = systemd_unit_name(home)
        self.unit = systemd_unit_path(self.label, home)
        self.pin_failed = False
        self._run = run or _subprocess_run

    def listeners(self) -> list[Listener]:
        return live_listeners()

    def packaged(self) -> PackagedService:
        result = self._run(
            [
                "systemctl",
                "--user",
                "show",
                self.label,
                "-p",
                "ActiveState",
                "-p",
                "MainPID",
                "-p",
                "LoadState",
            ]
        )
        shown = parse_systemctl_show(result.stdout)
        if self.unit.is_file() and not shown.installed:
            return PackagedService(installed=True, running=shown.running, pid=shown.pid)
        return shown

    def install_and_start(self, unit_text: str) -> None:
        self.unit.parent.mkdir(parents=True, exist_ok=True)
        self.unit.write_text(unit_text, encoding="utf-8")
        os.chmod(self.unit, 0o644)
        for argv in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", self.label],
        ):
            result = self._run(argv)
            if result.returncode != 0:
                self.unit.unlink(missing_ok=True)
                self._run(["systemctl", "--user", "daemon-reload"])
                detail = (result.stderr or result.stdout).strip()
                raise ServiceError(_service_failure("systemctl", detail))

    def stop_and_remove(self) -> None:
        if self.label == LEGACY_SYSTEMD_UNIT:
            self.pin_failed = not pin_legacy_service()
        self._run(["systemctl", "--user", "disable", "--now", self.label])
        if self.unit.is_file():
            self.unit.unlink()
        self._run(["systemctl", "--user", "daemon-reload"])


def build_platform() -> LaunchdPlatform | SystemdPlatform:
    home = Path.home()
    if sys.platform == "darwin":
        return LaunchdPlatform(home=home)
    if sys.platform.startswith("linux"):
        return SystemdPlatform(home=home)
    raise ServiceError("The packaged service supports macOS and Linux. Use ./nexus run.")


def dashboard_restart_advice() -> dict[str, str]:
    """Why the dashboard must not restart the door it is served from.

    The safe restart lives in the menu bar, which is another process: it runs
    ``./nexus stop`` and then ``./nexus start``. Doing that here would
    unload the unit and kill this process before start could run. There is no
    ``restart`` subcommand. This function does not call stop or start.
    """
    managed = (environ_get("NEXUS_SERVICE") or "").strip() == "1"
    command = "./nexus stop && ./nexus start"
    if sys.platform == "darwin":
        unit = "launchd"
    elif sys.platform.startswith("linux"):
        unit = "systemd"
    else:
        unit = "packaged"
    menu = ""
    if sys.platform == "darwin":
        menu = " On macOS the menu bar Restart door runs those same commands."
    if managed:
        return {
            "available": "no",
            "managed": "yes",
            "command": command,
            "note": (
                f"This door is the packaged {unit} service. A backend saved on this page "
                "is used on the next request. This page cannot restart the process: "
                "stop would unload the unit and this process would exit before start "
                f"could run. To reload environment variables, from another terminal "
                f"run {command}.{menu}"
            ),
        }
    return {
        "available": "no",
        "managed": "no",
        "command": "",
        "note": (
            "This is a foreground process, not a launchd or systemd unit, so there is "
            "no managed service to restart from here. A backend or model saved on this "
            "page is used on the next request. Stop this process and start it again to "
            "reload environment variables."
        ),
    }
