"""Nexus setup, service, and foreground commands."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from nexus import rabbit_cli
from nexus.config import (
    BACKEND_IDS,
    backend_choice_path,
    bridge_key_path,
    configured_port,
    environ_get,
    model_choice_from_file,
    model_for,
    private_mode,
    repo_root,
    service_backend,
)
from nexus.nous_token import NousTokenError, ensure_nous_token, needs_hermes_sync
from nexus.port_holder import current_service_pid
from nexus.service import (
    LEGACY_LAUNCHD_LABEL,
    LEGACY_SYSTEMD_UNIT,
    Outcome,
    ServiceError,
    build_platform,
    busy_message,
    door_environment,
    execute_start,
    execute_stop,
    live_listeners,
    render_installed_unit,
    venv_python,
)
from nexus.setup_flow import (
    API_BACKENDS,
    SUBSCRIPTION,
    SetupError,
    discovered_cli_bins,
    key_file_present,
    missing_cli_message,
    provider_note,
    resolved_cli,
    run_setup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description=(
            "Set up and run the local Nexus door on 127.0.0.1 (NEXUS_PORT sets the port)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="choose a backend and write key files")
    setup.add_argument(
        "--backend",
        help=(
            "openrouter (default), openai, anthropic, xai, chatgpt, codex, claude-code, "
            "copilot, grok-build, antigravity (opt-in: NEXUS_ENABLE_ANTIGRAVITY=1), "
            "or nous (advanced)"
        ),
    )
    setup.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="read one API key line from stdin",
    )
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="fail instead of prompting",
    )
    sub.add_parser(
        "start",
        help="install and start the packaged service when the NEXUS_PORT port is free",
    )
    sub.add_parser("stop", help="unload the packaged service; other listeners stay up")
    sub.add_parser("status", help="show local files and the packaged service")
    sub.add_parser("run", help="run in the foreground when the NEXUS_PORT port is free")
    check = sub.add_parser(
        "test",
        help="print the client settings and run a real chat and tool-call round trip",
    )
    check.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="keep retrying while nothing is listening yet (right after start)",
    )
    sub.add_parser("doctor", help="live readiness check (runs ./doctor.sh)")
    menubar = sub.add_parser(
        "menubar",
        help="optional macOS menu-bar status for the door",
    )
    menubar.add_argument(
        "--interval",
        type=float,
        help="seconds between health polls (default 5, minimum 1)",
    )
    rabbit_cli.add_parser(sub)
    return parser


def _emit(outcome: Outcome) -> int:
    stream = sys.stdout if outcome.code == 0 else sys.stderr
    print(outcome.message, file=stream)
    return outcome.code


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def _cmd_setup(args: argparse.Namespace) -> int:
    api_key = sys.stdin.readline() if args.api_key_stdin else None
    non_interactive = bool(args.non_interactive or not _stdin_is_tty())
    text = run_setup(backend=args.backend, api_key=api_key, non_interactive=non_interactive)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _require_backend() -> str:
    backend = service_backend()
    if backend not in BACKEND_IDS:
        raise ServiceError(
            f"The backend choice file {backend_choice_path()} is not a known backend. "
            "Run ./nexus setup."
        )
    return backend


def _cmd_start() -> int:
    python = venv_python()
    backend = _require_backend()
    cli_bins = discovered_cli_bins()
    if backend in SUBSCRIPTION and SUBSCRIPTION[backend].bin_env not in cli_bins:
        raise SetupError(missing_cli_message(backend))
    if needs_hermes_sync(backend):
        try:
            ensure_nous_token()
        except NousTokenError as exc:
            raise ServiceError(str(exc)) from exc
    model = (environ_get("NEXUS_MODEL") or "").strip() or None
    env = door_environment(backend, model=model, cli_bins=cli_bins)
    platform = build_platform()
    unit = render_installed_unit(platform.kind, repo=repo_root(), python=python, env=env)
    outcome = execute_start(platform, unit)
    message = outcome.message
    note = provider_note(backend)
    if note and outcome.code == 0:
        message = f"{message}\n{note}"
    return _emit(Outcome(outcome.code, message))


def _cmd_stop() -> int:
    return _emit(execute_stop(build_platform()))


def _file_line(label: str, path: Path, *, secret: bool) -> str:
    file_path = path
    if secret:
        if not key_file_present(file_path):
            return f"{label}: absent ({file_path})"
        mode = private_mode(file_path) or "unknown"
        return f"{label}: present mode {mode} ({file_path})"
    return f"{label}: {file_path}"


def _cmd_status() -> int:
    backend = service_backend()
    print(f"backend choice: {backend}")
    print(_file_line("choice file", backend_choice_path(), secret=False))
    saved_model = model_choice_from_file()
    print(f"model choice: {saved_model or 'built-in default'}")
    print(f"model in use: {model_for(backend)}")
    print(_file_line("bridge key", bridge_key_path(), secret=True))
    spec = API_BACKENDS.get(backend)
    if spec is not None:
        print(_file_line(f"{spec.label} key", spec.key_path(), secret=True))
    else:
        binary = resolved_cli(backend)
        if binary:
            print(f"{backend} CLI: {binary}")
        else:
            print(missing_cli_message(backend))
    platform = build_platform()
    print(f"packaged service: {platform.label}")
    if platform.label in {LEGACY_LAUNCHD_LABEL, LEGACY_SYSTEMD_UNIT}:
        print("  (pre-Nexus name, kept so nothing breaks; see docs/upgrading-from-os3-bridge.md)")
    service = platform.packaged()
    if service.running and service.pid:
        print(f"service state: running pid {service.pid}")
    elif service.installed:
        print("service state: installed, not running")
    else:
        print("service state: not installed")
    try:
        listeners = platform.listeners()
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if listeners:
        print(f"port {configured_port()}: in use")
        for item in listeners:
            print(f"  pid {item.pid} {item.command or 'unknown'}")
    else:
        print(f"port {configured_port()}: free")
    print("Check it with ./doctor.sh")
    return 0


def _default_model_ok() -> bool:
    """True when the default model may be served. Loud about it when not.

    Checked before exec'ing server.py so the operator sees the reason on the
    terminal they ran the command from, rather than only in a service log.
    """
    import asyncio

    from nexus.startup import DefaultModelUnavailable, enforce_default_model

    try:
        asyncio.run(enforce_default_model())
    except DefaultModelUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return False
    return True


def _cmd_run() -> int:
    service_mode = (environ_get("NEXUS_SERVICE") or "").strip() == "1"
    try:
        listeners = live_listeners()
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 0 if service_mode else 1
    if listeners:
        print(busy_message(listeners, service_pid=current_service_pid()), file=sys.stderr)
        return 0 if service_mode else 1
    if not _default_model_ok():
        return 0 if service_mode else 1
    server = repo_root() / "server.py"
    os.execv(sys.executable, [sys.executable, str(server)])
    return 1


def _cmd_test(args: argparse.Namespace) -> int:
    import asyncio

    from nexus.selftest import run_selftest

    return asyncio.run(run_selftest(wait=max(0.0, min(args.wait, 120.0))))


def _cmd_doctor() -> int:
    script = repo_root() / "doctor.sh"
    if not script.is_file():
        print(f"{script} is missing. Run ./doctor.sh from a full checkout.", file=sys.stderr)
        return 1
    sys.stdout.flush()
    os.execvp("bash", ["bash", str(script)])


def _cmd_menubar(args: argparse.Namespace) -> int:
    from nexus import menubar

    return menubar.main(["--interval", str(args.interval)] if args.interval else [])


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = _parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return _cmd_setup(args)
        if args.command == "start":
            return _cmd_start()
        if args.command == "stop":
            return _cmd_stop()
        if args.command == "status":
            return _cmd_status()
        if args.command == "run":
            return _cmd_run()
        if args.command == "test":
            return _cmd_test(args)
        if args.command == "doctor":
            return _cmd_doctor()
        if args.command == "menubar":
            return _cmd_menubar(args)
        if args.command == "rabbit":
            return rabbit_cli.run(args)
    except (SetupError, ServiceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
