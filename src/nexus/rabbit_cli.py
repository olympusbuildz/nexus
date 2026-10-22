"""``./nexus rabbit plan|setup|restore|verify``: human output over ``RabbitSetup``.

Exit codes: 0 done, 1 refused or failed, 3 manual steps needed. No output here carries the
bridge key; the key is only ever named by its file path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx

from nexus.config import configured_port
from nexus.rabbit_setup import RabbitError, RabbitSetup

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MANUAL = 3


def add_parser(sub: Any) -> None:
    rabbit = sub.add_parser(
        "rabbit",
        help="point Rabbit OS3 at this door (opt-in, undocumented web API)",
    )
    actions = rabbit.add_subparsers(dest="rabbit_command", required=True)
    actions.add_parser("plan", help="show what setup would do; changes nothing")
    setup = actions.add_parser("setup", help="register this door in OS3")
    setup.add_argument("--model", help="model id from this door's live list")
    setup.add_argument("--dry-run", action="store_true", help="show the change, make none")
    setup.add_argument(
        "--accept-no-rollback",
        action="store_true",
        help="go ahead even though OS3's current model is not in this door's list, so a "
        "failed setup could not put the current connection back",
    )
    actions.add_parser("restore", help="put back the OS3 connection saved before setup")
    actions.add_parser("verify", help="check OS3 points at this door")


async def _door_pulse() -> list[dict[str, Any]] | None:
    """Recent /v1 calls from the running door. None when it is not reachable."""
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            reply = await client.get(f"http://127.0.0.1:{configured_port()}/dashboard/pulse")
        entries = reply.json().get("entries")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return entries if isinstance(entries, list) else None


def _line_restore_key(info: dict[str, Any]) -> str:
    if info["source"] == "env":
        return f"  restore key:     file {info['path']} (NEXUS_RABBIT_RESTORE_KEY_FILE)"
    return (
        f"  restore key:     {info['candidates']} candidate file(s) in the key dirs; "
        "setup matches them to OS3's current key if it is not Local"
    )


def _lines_plan(plan: dict[str, Any], status: dict[str, Any]) -> list[str]:
    cdp = status.get("cdp", {})
    listening = {True: "listening", False: "not listening", None: "unknown"}[cdp.get("listening")]
    state = "on" if plan["opt_in"] else f"OFF (set {plan['opt_in_env']}=on to apply)"
    model = plan["model"] or "none chosen"
    if plan["model"]:
        model += f"  [{plan['model_source']}, {plan['billing']}]"
    key_state = "present" if plan["key_present"] else "MISSING"
    node_state = "present" if status["node_id"]["present"] else "MISSING"
    profile_state = "found" if cdp.get("profile_exists") else "not created yet"
    lines = [
        f"Rabbit OS3 setup plan   opt-in: {state}",
        f"  base URL:        {plan['base_url']}",
        f"  bridge key:      file {plan['key_file']} ({key_state})",
        f"  model:           {model}",
        f"  context window:  {plan['context_window']}",
        f"  node id:         {node_state} ({status['node_id']['path']})",
        _line_restore_key(plan["restore_key"]),
        f"  Chrome (CDP):    127.0.0.1:{cdp.get('port')} {listening}",
        f"  Chrome profile:  {cdp.get('profile_dir')} ({profile_state})",
    ]
    last = status.get("last_apply")
    if last:
        lines.append(f"  last setup:      {last.get('at')} with {last.get('model')}")
    for item in plan["warnings"]:
        lines.append(f"  warning: {item}")
    for item in plan["problems"]:
        lines.append(f"  problem: {item['message']} {item['hint']}")
    if not plan["automatic"]["possible"]:
        lines.append(f"  automatic setup blocked: {'; '.join(plan['automatic']['blocked_by'])}")
    lines.append("")
    lines.append("Next: NEXUS_RABBIT_SETUP=on ./nexus rabbit setup --dry-run")
    return lines


def _lines_diff(result: dict[str, Any]) -> list[str]:
    diff = result["diff"]
    before, after = diff["before"], diff["after"]
    lines = []
    if before is None:
        lines.append("  before: no connection")
    else:
        lines.append(
            f"  before: {before['provider']}  {before['endpoint']}  {before['modelId']}  "
            f"key {before['masked']}"
        )
    if after is None:
        lines.append("  after:  no connection")
    else:
        lines.append(
            "  after:  "
            + "  ".join(str(after[k]) for k in ("provider", "endpoint", "modelId") if k in after)
            + (f"  window {after['contextWindow']}" if "contextWindow" in after else "")
            + (f"  key {after['key']}" if "key" in after else "")
        )
    return lines


def _manual_lines(manual: dict[str, Any], url: str) -> list[str]:
    return [
        f"  open:           {url}",
        "  provider:       Local",
        f"  base URL:       {manual['base_url']}",
        f"  key:            contents of {manual['key_file']}",
        f"  model id:       {manual['model_id'] or '(pick one from the plan)'}",
        f"  context window: {manual['context_window']}",
    ]


def render_refusal(error: RabbitError) -> list[str]:
    lines = [f"{error.message} ({error.code})", error.hint]
    manual = error.extra.get("manual")
    if isinstance(manual, dict):
        lines += ["Values for a manual setup:", *_manual_lines(manual, error.extra["url"])]
    return lines


def render(result: dict[str, Any]) -> list[str]:
    action = result.get("action", "")
    if result.get("mode") == "manual":
        manual = result["manual"]
        return [
            f"Automatic {action} not possible: {result['error']} ({result['code']})",
            "Set it up by hand instead:",
            *_manual_lines(manual, result["url"]),
            *[f"  warning: {w}" for w in result.get("warnings", [])],
        ]
    lines: list[str] = []
    if not result["ok"]:
        lines.append(f"FAILED ({result.get('code')}): {result.get('error')}")
        if result.get("hint"):
            lines.append(f"  {result['hint']}")
        if isinstance(result.get("manual"), dict):
            lines += ["Values for a manual setup:", *_manual_lines(result["manual"], result["url"])]
        if "diff" in result:
            lines.extend(_lines_diff(result))
        return lines
    if action == "verify":
        lines.append("OS3 points at this door.")
        cur = result["current"]
        lines.append(f"  provider {cur['provider']}  {cur['endpoint']}  {cur['modelId']}")
        for name, value in result["checks"].items():
            lines.append(f"  {name}: {'n/a' if value is None else 'yes' if value else 'NO'}")
        status = result.get("call_status") or {}
        lines.append(
            f"  OS3 last call: {status.get('lastCallOutcome')} at {status.get('lastCallAt')}"
        )
        seen = result.get("nexus_saw_request")
        if seen is None:
            lines.append("  this door saw a request since setup: unknown")
        else:
            lines.append(
                f"  this door saw a request since {seen['since']}: "
                f"{'yes' if seen['v1'] else 'no'} (chat: {'yes' if seen['chat'] else 'no'})"
            )
        return lines
    head = {"apply": "Setup", "restore": "Restore"}.get(action, action)
    if result.get("dry_run"):
        lines.append("Dry run: nothing was changed. This is what setup would do.")
    elif result.get("changed"):
        lines.append(f"{head} done.")
    else:
        lines.append(result.get("note") or f"{head}: nothing to change.")
    lines.extend(_lines_diff(result))
    restore_key = result.get("restore_key") or {}
    if restore_key.get("source") == "auto-detected":
        lines.append(f"  restore key: auto-detected {restore_key['path']}")
    if result.get("backup"):
        lines.append(f"  backup of the previous connection: {result['backup']['path']}")
    lines.extend(f"  warning: {w}" for w in result.get("warnings", []))
    return lines


async def _run(args: argparse.Namespace) -> tuple[list[str], int]:
    engine = RabbitSetup(pulse_source=_door_pulse)
    command = args.rabbit_command
    if command == "plan":
        plan = await engine.plan()
        return _lines_plan(plan, await engine.status()), EXIT_OK if plan["ok"] else EXIT_FAILED
    if command == "setup":
        result = await engine.apply(
            model=args.model,
            dry_run=args.dry_run,
            accept_no_rollback=args.accept_no_rollback,
        )
    elif command == "restore":
        result = await engine.restore()
    else:
        result = await engine.verify()
    if result.get("mode") == "manual":
        return render(result), EXIT_MANUAL
    return render(result), EXIT_OK if result["ok"] else EXIT_FAILED


def run(args: argparse.Namespace) -> int:
    try:
        lines, code = asyncio.run(_run(args))
    except RabbitError as exc:
        print("\n".join(render_refusal(exc)), file=sys.stderr)
        return EXIT_FAILED
    print("\n".join(lines), file=sys.stdout if code == EXIT_OK else sys.stderr)
    return code
