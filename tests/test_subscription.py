"""Subscription CLIs. The binary is a local fake. No Codex, Claude, or network."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.cli_exec import CliResult, resolve_binary
from nexus.backends.subscription import (
    ANTIGRAVITY_MODELS,
    CLAUDE_CODE_MODELS,
    CODEX_MODELS,
    COPILOT_MODELS,
    AntigravityBackend,
    ClaudeCodeBackend,
    CodexCliBackend,
    CopilotCliBackend,
    GrokBuildBackend,
    _cli_model_list,
    _cli_model_slugs,
    _cli_resolved_model,
    build_turn,
    clear_probe_cache,
    feed_event,
    parse_cli_stdout,
)
from nexus.config import model_for

FAKE_CLI = """#!/usr/bin/env python3
import json
import os
import sys

log = os.environ.get("OS3_FAKE_CLI_LOG", "")
stdin = sys.stdin.read()
if log:
    images = []
    index = 0
    argv = sys.argv[1:]
    prompt_file = ""
    while index < len(argv) - 1:
        if argv[index] in {"--image", "--attachment"}:
            path = argv[index + 1]
            images.append(os.path.getsize(path) if os.path.isfile(path) else -1)
            index += 2
            continue
        if argv[index] == "--prompt-file":
            path = argv[index + 1]
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    prompt_file = handle.read()
            index += 2
            continue
        index += 1
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "argv": argv,
                    "openai": os.environ.get("OPENAI_API_KEY", ""),
                    "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
                    "xai": os.environ.get("XAI_API_KEY", ""),
                    "gemini": os.environ.get("GEMINI_API_KEY", ""),
                    "stdin": stdin,
                    "prompt_file": prompt_file,
                    "image_bytes": images,
                }
            )
            + "\\n"
        )
args = sys.argv[1:]
mode = os.environ.get("OS3_FAKE_CLI_MODE", "ok")
if args[:1] == ["--version"]:
    print("grok 1.0.0")
    raise SystemExit(0)
if args[:2] == ["debug", "models"]:
    if mode == "logged_out":
        sys.stderr.write("Not logged in\\n")
        raise SystemExit(1)
    print(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra"},
                    {"slug": "gpt-5.5-codex", "display_name": "GPT-5.5-Codex"},
                ]
            }
        )
    )
    raise SystemExit(0)
if args[:2] == ["login", "status"] or args[:2] == ["auth", "status"] or args[:1] in (
    ["version"],
    ["models"],
):
    if mode == "logged_out":
        sys.stderr.write("Not logged in\\n")
        raise SystemExit(1)
    if mode == "leak":
        sys.stdout.write("Logged in sk-proj-probe-should-not-leak user@example.com\\n")
        raise SystemExit(0)
    if mode == "grok_signed_out" and args[:1] == ["models"]:
        print("You are not authenticated.")
        print("")
        print("Default model: grok-4.5")
        print("")
        print("Available models:")
        print("  * grok-4.5 (default)")
        raise SystemExit(0)
    if mode == "grok_signed_in" and args[:1] == ["models"]:
        print("Default model: grok-4.5")
        print("")
        print("Available models:")
        print("  * grok-4.5 (default)")
        raise SystemExit(0)
    if args[:1] == ["models"]:
        print("You are logged in")
        print("gemini-3.8-flash-high")
    elif args[:1] == ["version"]:
        print("Logged in")
    elif args[0] == "auth":
        print('{"loggedIn": true, "authMethod": "claude.ai", "email": "user@example.com"}')
    else:
        print("Logged in using ChatGPT")
    raise SystemExit(0)
if mode == "logged_out":
    sys.stderr.write("Not logged in\\n")
    raise SystemExit(1)
if mode == "grok_signed_out":
    # What a real signed-out `grok` one-shot prints: no output, exit 1.
    sys.stderr.write("Not signed in. To authenticate without a browser, run:")
    sys.stderr.write("  grok login --device-code")
    sys.stderr.write("")
    sys.stderr.write("Alternatively, set the XAI_API_KEY environment variable or run")
    sys.stderr.write("`grok login` on a machine with a browser.")
    raise SystemExit(1)
if mode == "fail":
    sys.stderr.write("codex exploded sk-proj-chat-should-not-leak\\n")
    raise SystemExit(1)
if mode == "limit":
    sys.stderr.write("usage limit reached\\n")
    raise SystemExit(1)
if (
    "--input-format" in args
    and args[args.index("--input-format") + 1] == "stream-json"
    and "-p" not in args
):
    output = "json"
    if "--output-format" in args:
        output = args[args.index("--output-format") + 1]
    if output == "stream-json":
        print(
            '{"event":"step_update","step_update":{"step_type":"agent_response",'
            '"state":"ACTIVE","text_delta":"OS3_BRIDGE_OK"}}',
            flush=True,
        )
        print(
            '{"event":"result","result":{"status":"SUCCESS","response":"OS3_BRIDGE_OK",'
            '"usage":{"input_tokens":3,"output_tokens":2}}}',
            flush=True,
        )
    else:
        print(
            '{"status":"SUCCESS","response":"OS3_BRIDGE_OK",'
            '"usage":{"input_tokens":3,"output_tokens":2}}',
            flush=True,
        )
    raise SystemExit(0)
if "--no-auto-update" in args or "--prompt-file" in args:
    output = "json"
    if "--output-format" in args:
        output = args[args.index("--output-format") + 1]
    if output == "streaming-json":
        print('{"type":"text","data":"OS3_BRIDGE_OK"}', flush=True)
        print(
            '{"type":"end","usage":{"input_tokens":5,"output_tokens":2}}',
            flush=True,
        )
    else:
        print(
            '{"text":"OS3_BRIDGE_OK","usage":{"input_tokens":5,"output_tokens":2}}',
            flush=True,
        )
    raise SystemExit(0)
if "-s" in args and "--output-format" in args and args[args.index("--output-format") + 1] == "json":
    print(
        '{"type":"assistant.message_delta","data":{"deltaContent":"OS3_BRIDGE_OK"}}',
        flush=True,
    )
    print(
        '{"type":"assistant.message","data":{"content":"OS3_BRIDGE_OK",'
        '"usage":{"input_tokens":3,"output_tokens":2}}}',
        flush=True,
    )
    raise SystemExit(0)
if "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json":
    print(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"OS3_BRIDGE_OK"}}}',
        flush=True,
    )
    print(
        '{"type":"result","is_error":false,"result":"OS3_BRIDGE_OK",'
        '"usage":{"input_tokens":2,"output_tokens":1}}',
        flush=True,
    )
    raise SystemExit(0)
if args and args[0] == "exec":
    print('{"type":"thread.started","thread_id":"thr_test"}', flush=True)
    print(
        '{"type":"item.completed","item":{"type":"agent_message","text":"OS3_BRIDGE_OK"}}',
        flush=True,
    )
    print(
        '{"type":"turn.completed","usage":{"input_tokens":4,"output_tokens":2}}',
        flush=True,
    )
    raise SystemExit(0)
print(
    '{"type":"result","is_error":false,"result":"OS3_BRIDGE_OK",'
    '"usage":{"input_tokens":2,"output_tokens":1}}',
    flush=True,
)
"""


def _install_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str = "ok") -> Path:
    binary = tmp_path / "fake-cli"
    binary.write_text(FAKE_CLI, encoding="utf-8")
    binary.chmod(0o755)
    log = tmp_path / "cli-log.jsonl"
    monkeypatch.setenv("OS3_FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("OS3_FAKE_CLI_MODE", mode)
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(binary))
    monkeypatch.setenv("OS3_BRIDGE_CLAUDE_BIN", str(binary))
    clear_probe_cache()
    return log


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_feed_event_dedupes_codex_final_message() -> None:
    state: dict = {"text": "", "usage": {}}
    first = feed_event(
        "codex",
        {
            "type": "event_msg",
            "payload": {"type": "agent_message_content_delta", "delta": "OS3_"},
        },
        state,
    )
    second = feed_event(
        "codex",
        {"type": "item.completed", "item": {"type": "agent_message", "text": "OS3_BRIDGE_OK"}},
        state,
    )
    usage = feed_event(
        "codex",
        {"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}},
        state,
    )
    assert first.delta == "OS3_"
    assert second.delta == "BRIDGE_OK"
    assert usage.delta == ""
    assert state["text"] == "OS3_BRIDGE_OK"
    assert state["usage"]["output_tokens"] == 2


def test_parse_claude_json_result() -> None:
    text, usage, error = parse_cli_stdout(
        "claude",
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "OS3_BRIDGE_OK",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        ),
    )
    assert error is None
    assert text == "OS3_BRIDGE_OK"
    assert usage == {"input_tokens": 2, "output_tokens": 1}


def test_codex_probe_and_chat(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-parent-should-not-pass")
    with TestClient(create_app()) as client:
        health = client.get("/health")
        doctor = client.get("/doctor")
        models = client.get("/v1/models", headers=auth_header)
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "Reply with exactly: ok"}]},
        )
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "model": "gpt-5.6-sol",
                "messages": [{"role": "user", "content": "stream please"}],
                "stream": True,
            },
        )
        messages = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "gpt-5.6-sol",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "messages please"}],
            },
        )
        streamed_messages = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "gpt-5.6-sol",
                "max_tokens": 16,
                "stream": True,
                "messages": [{"role": "user", "content": "messages stream"}],
            },
        )
    body = health.json()
    assert body["backend"] == "codex"
    assert body["backends"]["codex"]["implemented"] is True
    assert body["backends"]["codex"]["ready"] is True
    assert body["backends"]["codex"]["login"] == "ok"
    assert body["backends"]["codex"]["probe"] == "login status"
    assert body["backends"]["codex"]["models"] == ["gpt-6-astra", "gpt-5.5-codex"]
    assert body["backends"]["codex"]["models_source"] == "cli"
    assert body["routes"]["/v1/messages"]["mode"] == "translate"
    assert doctor.json()["backends"]["codex"]["ready"] is True
    leaked = "sk-proj-parent-should-not-pass"
    assert leaked not in health.text
    assert leaked not in doctor.text
    assert "user@example.com" not in health.text
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "gpt-6-astra"
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert chat.json()["usage"]["prompt_tokens"] == 4
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert "OS3_BRIDGE_OK" in streamed.text
    assert "data: [DONE]" in streamed.text
    assert messages.status_code == 200
    assert messages.json()["type"] == "message"
    assert messages.json()["content"][0]["text"] == "OS3_BRIDGE_OK"
    assert streamed_messages.status_code == 200
    assert streamed_messages.headers["content-type"].startswith("text/event-stream")
    assert "event: message_start" in streamed_messages.text
    assert "OS3_BRIDGE_OK" in streamed_messages.text
    calls = _calls(log)
    assert calls[0]["argv"][:2] == ["login", "status"]
    execs = [call for call in calls if call["argv"][:1] == ["exec"]]
    assert execs
    argv = execs[0]["argv"]
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert "read-only" in argv
    assert "-a" not in argv
    assert argv[argv.index("-c") + 1] == 'approval_policy="never"'
    assert "--image" not in argv
    assert "-m" in argv
    assert "gpt-5.6-sol" in argv
    assert execs[0]["openai"] == ""
    assert "Reply with exactly: ok" in execs[0]["stdin"]
    assert leaked not in log.read_text(encoding="utf-8")
    assert "auth.json" not in log.read_text(encoding="utf-8")


def test_probe_stdout_is_not_copied_into_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_cli(tmp_path, monkeypatch, mode="leak")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with TestClient(create_app()) as client:
        health = client.get("/health")
        doctor = client.get("/doctor")
    assert health.status_code == 200
    assert health.json()["backends"]["codex"]["implemented"] is True
    assert health.json()["backends"]["codex"]["ready"] is True
    for payload in (health.text, doctor.text):
        assert "sk-proj-probe-should-not-leak" not in payload
        assert "user@example.com" not in payload


def test_resolve_binary_finds_brew_prefix_when_path_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brew = tmp_path / "opt" / "homebrew" / "bin"
    local = tmp_path / "usr" / "local" / "bin"
    brew.mkdir(parents=True)
    local.mkdir(parents=True)
    binary = brew / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    plain = local / "claude"
    plain.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.delenv("OS3_BRIDGE_CODEX_BIN")
    monkeypatch.delenv("OS3_BRIDGE_CLAUDE_BIN")
    monkeypatch.setattr(
        "nexus.backends.cli_exec.BREW_BIN_PREFIXES",
        (str(brew), str(local)),
    )
    assert resolve_binary("OS3_BRIDGE_CODEX_BIN", "codex") == str(binary)
    assert resolve_binary("OS3_BRIDGE_CLAUDE_BIN", "claude") is None
    plain.chmod(0o755)
    assert resolve_binary("OS3_BRIDGE_CLAUDE_BIN", "claude") == str(plain)
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(tmp_path / "missing-codex"))
    assert resolve_binary("OS3_BRIDGE_CODEX_BIN", "codex") is None


def test_resolve_binary_finds_grok_home_when_path_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grok_dir = tmp_path / ".grok" / "bin"
    grok_dir.mkdir(parents=True)
    binary = grok_dir / "grok"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.delenv("OS3_BRIDGE_GROK_BIN")
    monkeypatch.setattr("nexus.backends.cli_exec.BREW_BIN_PREFIXES", ())
    monkeypatch.setattr("nexus.backends.cli_exec.grok_bin_dir", lambda: grok_dir)
    assert resolve_binary("OS3_BRIDGE_GROK_BIN", "grok") == str(binary)
    monkeypatch.setenv("OS3_BRIDGE_GROK_BIN", str(tmp_path / "missing-grok"))
    assert resolve_binary("OS3_BRIDGE_GROK_BIN", "grok") is None


def test_health_cli_present_when_brew_binary_is_off_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brew = tmp_path / "opt" / "homebrew" / "bin"
    brew.mkdir(parents=True)
    binary = brew / "codex"
    binary.write_text(FAKE_CLI, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_FAKE_CLI_LOG", str(tmp_path / "cli-log.jsonl"))
    monkeypatch.setenv("OS3_FAKE_CLI_MODE", "logged_out")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.delenv("OS3_BRIDGE_CODEX_BIN")
    monkeypatch.setattr(
        "nexus.backends.cli_exec.BREW_BIN_PREFIXES",
        (str(brew), str(tmp_path / "usr" / "local" / "bin")),
    )
    clear_probe_cache()
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
    info = health["backends"]["codex"]
    assert info["cli_present"] is True
    assert info["cli_path"] == str(binary)
    assert info["ready"] is False
    assert info["login"] == "missing"
    for name in ("claude-code", "copilot", "grok-build", "antigravity"):
        assert health["backends"][name]["cli_present"] is False
        assert health["backends"][name]["ready"] is False


def test_health_grok_present_from_home_bin_without_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grok_dir = tmp_path / ".grok" / "bin"
    grok_dir.mkdir(parents=True)
    binary = grok_dir / "grok"
    binary.write_text(FAKE_CLI, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_FAKE_CLI_LOG", str(tmp_path / "cli-log.jsonl"))
    monkeypatch.setenv("OS3_FAKE_CLI_MODE", "grok_signed_out")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.delenv("OS3_BRIDGE_GROK_BIN")
    monkeypatch.setattr("nexus.backends.cli_exec.BREW_BIN_PREFIXES", ())
    monkeypatch.setattr("nexus.backends.cli_exec.grok_bin_dir", lambda: grok_dir)
    clear_probe_cache()
    with TestClient(create_app()) as client:
        info = client.get("/health").json()["backends"]["grok-build"]
    assert info["cli_present"] is True
    assert info["cli_path"] == str(binary)
    assert info["ready"] is False
    assert info["login"] == "missing"


def test_logged_out_probe_is_implemented_but_not_ready(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_cli(tmp_path, monkeypatch, mode="logged_out")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert health["backends"]["codex"]["implemented"] is True
    assert health["backends"]["codex"]["ready"] is False
    assert health["backends"]["codex"]["login"] == "missing"
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "codex_login_required"
    assert "sk-" not in chat.text


def test_cli_failure_is_redacted(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_cli(tmp_path, monkeypatch, mode="fail")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with TestClient(create_app()) as client:
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert chat.status_code == 502
    assert chat.json()["error"]["code"] == "codex_failed"
    assert "sk-proj-chat-should-not-leak" not in chat.text


def test_usage_limit_is_429(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_cli(tmp_path, monkeypatch, mode="limit")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with TestClient(create_app()) as client:
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert chat.status_code == 429
    assert chat.json()["error"]["code"] == "codex_usage_limit"


def test_tools_are_refused_without_exec(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with TestClient(create_app()) as client:
        tools = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "x"}}],
            },
        )
    assert tools.status_code == 400
    assert tools.json()["error"]["code"] == "tools_not_supported"
    assert _calls(log) == []


def test_claude_alias_stays_on_the_api(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "claude")
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert health["backend"] == "anthropic"
    assert health["backends"]["anthropic"]["implemented"] is True
    assert health["backends"]["anthropic"]["ready"] is False
    assert health["routes"]["/v1/messages"]["mode"] == "native"
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "anthropic_key_missing"


def test_claude_cli_alias_streams_messages(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "claude-cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent-should-not-pass")
    with TestClient(create_app()) as client:
        health = client.get("/health")
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    body = health.json()
    assert body["backend"] == "claude-code"
    assert body["backends"]["claude-code"]["implemented"] is True
    assert body["backends"]["claude-code"]["ready"] is True
    assert body["backends"]["claude-code"]["probe"] == "auth status"
    assert body["backends"]["anthropic"].get("credential") != "cli-login"
    assert "sk-ant-parent-should-not-pass" not in health.text
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert streamed.status_code == 200
    assert "OS3_BRIDGE_OK" in streamed.text
    assert "data: [DONE]" in streamed.text
    calls = _calls(log)
    assert any(call["argv"][:2] == ["auth", "status"] for call in calls)
    prints = [call for call in calls if "-p" in call["argv"]]
    assert prints
    assert prints[0]["anthropic"] == ""
    assert "--output-format" in prints[0]["argv"]
    assert "json" in prints[0]["argv"]
    streamed_calls = [call for call in prints if "stream-json" in call["argv"]]
    assert streamed_calls
    assert "--include-partial-messages" in streamed_calls[0]["argv"]
    assert "--input-format" not in prints[0]["argv"]
    assert "sk-ant-parent-should-not-pass" not in log.read_text(encoding="utf-8")


PNG = base64.b64encode(b"png-bytes").decode()
DATA_PNG = f"data:image/png;base64,{PNG}"


def _shot_message() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is on screen?"},
            {"type": "image_url", "image_url": {"url": DATA_PNG}},
        ],
    }


def test_codex_attaches_inline_screenshots(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with TestClient(create_app()) as client:
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [_shot_message()]},
        )
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [_shot_message()], "stream": True},
        )
        messages = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "gpt-5.6-sol",
                "max_tokens": 16,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "screen"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": PNG,
                                },
                            },
                        ],
                    }
                ],
            },
        )
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert streamed.status_code == 200
    assert "OS3_BRIDGE_OK" in streamed.text
    assert messages.status_code == 200
    assert messages.json()["content"][0]["text"] == "OS3_BRIDGE_OK"
    execs = [call for call in _calls(log) if call["argv"][:1] == ["exec"]]
    assert len(execs) == 3
    for call in execs:
        argv = call["argv"]
        assert argv.count("--image") == 1
        path = argv[argv.index("--image") + 1]
        assert path.endswith(".png")
        assert argv.index("--image") < argv.index("--")
        assert call["image_bytes"] == [len(b"png-bytes")]
        assert PNG not in call["stdin"]
        assert "[image]" in call["stdin"]
    assert "what is on screen?" in execs[0]["stdin"]
    assert "screen" in execs[2]["stdin"]


def test_claude_code_sends_screenshot_blocks(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "claude-code")
    with TestClient(create_app()) as client:
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [_shot_message()]},
        )
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [_shot_message()], "stream": True},
        )
    assert chat.status_code == 200
    assert streamed.status_code == 200
    assert "OS3_BRIDGE_OK" in streamed.text
    prints = [call for call in _calls(log) if "-p" in call["argv"]]
    assert len(prints) == 2
    for call in prints:
        argv = call["argv"]
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert argv[argv.index("--tools") + 1] == ""
        body = json.loads(call["stdin"])
        images = [block for block in body["message"]["content"] if block["type"] == "image"]
        texts = [block for block in body["message"]["content"] if block["type"] == "text"]
        assert images[0]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": PNG,
        }
        assert texts
        assert "what is on screen?" in texts[0]["text"]
        assert "[image]" not in texts[0]["text"]
    assert "stream-json" == prints[1]["argv"][prints[1]["argv"].index("--output-format") + 1]
    assert "--include-partial-messages" in prints[1]["argv"]


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
def test_remote_image_urls_are_not_fetched(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    log = _install_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", backend)
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/shot.png"},
                            }
                        ],
                    }
                ]
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "image_url_unsupported"
    assert _calls(log) == []


def test_cli_turn_keeps_screenshot_order(tmp_path: Path) -> None:
    jpeg = base64.b64encode(b"jpeg-bytes").decode()
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": DATA_PNG}},
                    {"type": "text", "text": "and"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg}"}},
                ],
            }
        ]
    }
    codex = build_turn(payload, preamble=False, family="codex", cwd=str(tmp_path))
    assert [Path(path).name for path in codex.image_paths] == ["shot-1.png", "shot-2.jpg"]
    assert Path(codex.image_paths[0]).read_bytes() == b"png-bytes"
    assert Path(codex.image_paths[1]).read_bytes() == b"jpeg-bytes"
    assert codex.stream_json_input is False
    assert PNG not in codex.stdin
    claude = build_turn(payload, preamble=False, family="claude", cwd=str(tmp_path / "unused"))
    body = json.loads(claude.stdin)
    content = body["message"]["content"]
    assert [block["type"] for block in content] == ["image", "text", "image"]
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[2]["source"]["media_type"] == "image/jpeg"
    assert content[1]["text"] == "user:\nand"
    assert claude.image_paths == ()
    assert claude.stream_json_input is True


def _install_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    *,
    mode: str = "ok",
) -> Path:
    log = _install_cli(tmp_path, monkeypatch, mode=mode)
    for name, filename in (
        ("OS3_BRIDGE_CODEX_BIN", "missing-codex"),
        ("OS3_BRIDGE_CLAUDE_BIN", "missing-claude"),
        ("OS3_BRIDGE_COPILOT_BIN", "missing-copilot"),
        ("OS3_BRIDGE_GROK_BIN", "missing-grok"),
        ("OS3_BRIDGE_AGY_BIN", "missing-agy"),
    ):
        monkeypatch.setenv(name, str(tmp_path / filename))
    monkeypatch.setenv(env_name, str(tmp_path / "fake-cli"))
    clear_probe_cache()
    return log


def test_feed_new_cli_families() -> None:
    state: dict = {"text": "", "usage": {}}
    delta = feed_event(
        "copilot",
        {"type": "assistant.message_delta", "data": {"deltaContent": "OS3_"}},
        state,
    )
    final = feed_event(
        "copilot",
        {
            "type": "assistant.message",
            "data": {"content": "OS3_BRIDGE_OK", "usage": {"input_tokens": 3, "output_tokens": 2}},
        },
        state,
    )
    assert delta.delta == "OS3_"
    assert final.delta == "BRIDGE_OK"
    assert state["usage"]["input_tokens"] == 3
    grok_state: dict = {"text": "", "usage": {}}
    assert feed_event("grok", {"type": "text", "data": "OS3_BRIDGE_OK"}, grok_state).delta == (
        "OS3_BRIDGE_OK"
    )
    agy_state: dict = {"text": "", "usage": {}}
    piece = feed_event(
        "antigravity",
        {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "text_delta": "OS3_"},
        },
        agy_state,
    )
    done = feed_event(
        "antigravity",
        {"status": "SUCCESS", "response": "OS3_BRIDGE_OK", "usage": {"output_tokens": 2}},
        agy_state,
    )
    assert piece.delta == "OS3_"
    assert done.delta == "BRIDGE_OK"
    assert agy_state["text"] == "OS3_BRIDGE_OK"


@pytest.mark.parametrize(
    ("backend", "env_name", "probe", "model"),
    [
        ("copilot", "OS3_BRIDGE_COPILOT_BIN", "version", "claude-sonnet-4.6"),
        ("github-copilot", "OS3_BRIDGE_COPILOT_BIN", "version", "claude-sonnet-4.6"),
        ("grok-build", "OS3_BRIDGE_GROK_BIN", "models", "grok-build"),
        ("grok-cli", "OS3_BRIDGE_GROK_BIN", "models", "grok-build"),
        ("antigravity", "OS3_BRIDGE_AGY_BIN", "models", "gemini-3.8-flash-high"),
        ("agy", "OS3_BRIDGE_AGY_BIN", "models", "gemini-3.8-flash-high"),
    ],
)
def test_new_subscription_cli_chat(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    env_name: str,
    probe: str,
    model: str,
) -> None:
    log = _install_named(tmp_path, monkeypatch, env_name)
    monkeypatch.setenv("OS3_BRIDGE_ENABLE_ANTIGRAVITY", "1")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", backend)
    monkeypatch.setenv("XAI_API_KEY", "xai-parent-should-not-pass")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-parent-should-not-pass")
    monkeypatch.setenv("GH_TOKEN", "gh-token-parent-should-not-leak")
    canonical = {
        "github-copilot": "copilot",
        "grok-cli": "grok-build",
        "agy": "antigravity",
    }.get(backend, backend)
    with TestClient(create_app()) as client:
        health = client.get("/health")
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "Reply with exactly: ok"}]},
        )
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "stream please"}], "stream": True},
        )
        messages = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "messages please"}],
            },
        )
        tools = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "x"}}],
            },
        )
    body = health.json()
    assert body["backend"] == canonical
    assert body["backends"]["xai"]["id"] == "xai"
    info = body["backends"][canonical]
    assert info["implemented"] is True
    assert info["probe"] == probe
    assert info["ready"] is True
    assert info["login"] == "ok"
    assert info["models_source"] == "catalog"
    assert "gh-token-parent-should-not-leak" not in health.text
    assert "user@example.com" not in health.text
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert streamed.status_code == 200
    assert "OS3_BRIDGE_OK" in streamed.text
    assert "data: [DONE]" in streamed.text
    assert messages.status_code == 200
    assert messages.json()["content"][0]["text"] == "OS3_BRIDGE_OK"
    assert tools.status_code == 400
    assert tools.json()["error"]["code"] == "tools_not_supported"
    calls = _calls(log)
    assert calls[0]["argv"][:1] == [probe]
    chats = [call for call in calls if call["argv"][:1] != [probe]]
    assert chats
    assert chats[0]["xai"] == ""
    assert chats[0]["gemini"] == ""
    if canonical == "copilot":
        assert "-s" in chats[0]["argv"]
        assert "--no-ask-user" in chats[0]["argv"]
        assert "-p" not in chats[0]["argv"]
        assert "Reply with exactly: ok" in chats[0]["stdin"]
        assert "gh-token-parent-should-not-leak" not in log.read_text(encoding="utf-8")
    elif canonical == "grok-build":
        prompt_at = chats[0]["argv"].index("-p") + 1
        assert "Reply with exactly: ok" in chats[0]["argv"][prompt_at]
        assert "--prompt-file" not in chats[0]["argv"]
        assert chats[0]["argv"][chats[0]["argv"].index("--sandbox") + 1] == "read-only"
        assert chats[0]["argv"][chats[0]["argv"].index("--output-format") + 1] == "json"
        streamed_calls = [call for call in chats if "streaming-json" in call["argv"]]
        assert streamed_calls
    else:
        assert chats[0]["argv"][chats[0]["argv"].index("--input-format") + 1] == "stream-json"
        assert chats[0]["argv"][chats[0]["argv"].index("--output-format") + 1] == "stream-json"
        assert "--sandbox" in chats[0]["argv"]
        body_in = json.loads(chats[0]["stdin"])
        assert body_in["event"] == "user"
        assert "Reply with exactly: ok" in body_in["message"]["content"]
        assert all(
            call["argv"][call["argv"].index("--output-format") + 1] == "stream-json"
            for call in chats
        )


def test_copilot_attaches_inline_screenshots(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_COPILOT_BIN")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "copilot")
    with TestClient(create_app()) as client:
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [_shot_message()]},
        )
    assert chat.status_code == 200
    calls = [call for call in _calls(log) if "--attachment" in call["argv"]]
    assert len(calls) == 1
    argv = calls[0]["argv"]
    path = argv[argv.index("--attachment") + 1]
    assert path.endswith(".png")
    assert calls[0]["image_bytes"] == [len(b"png-bytes")]
    assert PNG not in calls[0]["stdin"]
    assert "[image]" in calls[0]["stdin"]


@pytest.mark.parametrize("backend", ["grok-build", "antigravity"])
def test_text_only_clis_refuse_images(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    env_name = "OS3_BRIDGE_GROK_BIN" if backend == "grok-build" else "OS3_BRIDGE_AGY_BIN"
    log = _install_named(tmp_path, monkeypatch, env_name)
    monkeypatch.setenv("OS3_BRIDGE_ENABLE_ANTIGRAVITY", "1")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", backend)
    with TestClient(create_app()) as client:
        inline = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [_shot_message()]},
        )
        remote = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/shot.png"},
                            }
                        ],
                    }
                ]
            },
        )
    assert inline.status_code == 400
    assert inline.json()["error"]["code"] == "images_not_supported"
    assert remote.status_code == 400
    assert remote.json()["error"]["code"] == "images_not_supported"
    assert _calls(log) == []


def test_copilot_login_state_uses_probe_text_not_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CopilotCliBackend()
    started = CliResult(0, "copilot 1.2.3\n", "", True)
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert backend._login_state(started) == "unusable"
    monkeypatch.setenv("GH_TOKEN", "gh-token-value-not-stored")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-token-value-not-stored")
    assert backend._login_state(started) == "unusable"
    signed = CliResult(0, "Logged in as octocat\n", "", True)
    assert backend._login_state(signed) == "ok"
    denied = CliResult(1, "", "Not logged in\n", True)
    assert backend._login_state(denied) == "missing"


def test_model_catalog_is_not_a_login() -> None:
    catalog = CliResult(0, "gemini-3.8-flash-high Gemini 3.8 Flash\n", "", True)
    assert AntigravityBackend()._login_state(catalog) == "unusable"
    told = CliResult(1, "", "Run grok login first\n", True)
    assert GrokBuildBackend()._login_state(told) == "missing"


def test_grok_probe_reads_the_unauthenticated_line() -> None:
    """`grok models` is the probe, and its unauthenticated line decides."""
    backend = GrokBuildBackend()
    assert GrokBuildBackend.probe_args == ("models",)
    # Real `grok models` output, unauthenticated: exit 0 and still a login line.
    signed_out = CliResult(
        0,
        "You are not authenticated.\n\nDefault model: grok-4.5\n\n"
        "Available models:\n  * grok-4.5 (default)\n",
        "",
        True,
    )
    assert backend._login_state(signed_out) == "missing"
    signed_in = CliResult(
        0,
        "Default model: grok-4.5\n\nAvailable models:\n  * grok-4.5 (default)\n",
        "",
        True,
    )
    assert backend._login_state(signed_in) == "ok"
    assert backend._login_state(CliResult(0, "", "You are not authenticated.\n", True)) == "missing"
    assert backend._login_state(CliResult(1, "You are not authenticated.\n", "", True)) == "missing"


def test_installed_cli_names_stay_on_their_backends() -> None:
    assert CodexCliBackend.command == "codex"
    assert CodexCliBackend.probe_args == ("login", "status")
    assert CodexCliBackend.login_command == "codex login"
    assert ClaudeCodeBackend.command == "claude"
    assert ClaudeCodeBackend.id == "claude-code"
    assert ClaudeCodeBackend.probe_args == ("auth", "status")
    assert ClaudeCodeBackend.login_command == "claude auth login"
    assert CopilotCliBackend.command == "copilot"
    assert GrokBuildBackend.command == "grok"
    assert GrokBuildBackend.id == "grok-build"
    assert GrokBuildBackend.probe_args == ("models",)
    assert AntigravityBackend.command == "agy"
    assert AntigravityBackend.id == "antigravity"


def test_antigravity_is_off_until_opted_in(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_AGY_BIN")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "antigravity")
    with TestClient(create_app()) as client:
        row = client.get("/health").json()["backends"]["antigravity"]
        doctor = client.get("/doctor").json()
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert row["ready"] is False
    assert row["enabled"] is False
    assert row["cli_present"] is True
    assert "NEXUS_ENABLE_ANTIGRAVITY=1" in row["hint"]
    assert "NEXUS_ENABLE_ANTIGRAVITY=1" in doctor["backends"]["antigravity"]["hint"]
    assert chat.status_code == 403
    error = chat.json()["error"]
    assert error["code"] == "backend_disabled"
    assert "NEXUS_ENABLE_ANTIGRAVITY=1" in error["hint"]
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_deprecated_gemini_cli_is_not_antigravity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "gemini")
    with TestClient(create_app()) as client:
        health = client.get("/health")
    body = health.json()
    assert health.status_code == 200
    assert body["backend"] == "gemini"
    assert body["backend_known"] is False
    assert "gemini" not in body["backends"]
    assert body["backends"]["antigravity"]["command"] == "agy"
    assert body["backends"]["grok-build"]["command"] == "grok"
    assert body["backends"]["xai"]["id"] == "xai"


def test_grok_alias_stays_on_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "grok")
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
    assert health["backend"] == "xai"
    assert health["backends"]["grok-build"]["cli_present"] is False


def test_logged_out_new_cli_is_not_ready(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN", mode="grok_signed_out")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "grok-build")
    with TestClient(create_app()) as client:
        response = client.get("/health")
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    health = response.json()
    assert health["backends"]["grok-build"]["implemented"] is True
    assert health["backends"]["grok-build"]["ready"] is False
    assert health["backends"]["grok-build"]["login"] == "missing"
    assert health["backends"]["grok-build"]["probe"] == "models"
    assert "not authenticated" not in response.text
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "grok-build_login_required"


def test_cli_resolved_model_reads_the_probe_output() -> None:
    """`cli_model` is what the CLI's own command printed, never a guess."""
    grok_out = (
        "You are not authenticated.\n\nDefault model: grok-4.5\n\n"
        "Available models:\n  * grok-4.5 (default)\n"
    )
    assert _cli_resolved_model(grok_out) == "grok-4.5"
    # `agy models` marks the default with a starred entry instead.
    agy_out = "Available models:\n  * gemini-3.8-flash-high (default)\n  gemini-3.1-pro-high\n"
    assert _cli_resolved_model(agy_out) == "gemini-3.8-flash-high"
    assert _cli_resolved_model("Default model: \n") is None
    assert _cli_resolved_model("Logged in using ChatGPT\n") is None
    assert _cli_resolved_model("Default model: user@example.com\n") is None
    assert _cli_resolved_model("Default model: [REDACTED:openai_key]\n") is None
    assert _cli_resolved_model("pick a default model: later\n") is None
    assert _cli_resolved_model("") is None


def test_cli_model_is_absent_for_api_backends_and_kept_off_the_login_probes(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """grok reports one; codex's `login status` cannot, so it stays null."""
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN", mode="grok_signed_in")
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
    backends = health["backends"]
    assert backends["grok-build"]["cli_model"] == "grok-4.5"
    assert backends["codex"]["cli_model"] is None
    assert backends["claude-code"]["cli_model"] is None
    assert backends["copilot"]["cli_model"] is None
    for name in ("openrouter", "openai", "anthropic", "xai", "nous"):
        assert "cli_model" not in backends[name]


def test_logged_out_cli_still_reports_its_resolved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`grok models` names a default model even while signed out."""
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN", mode="grok_signed_out")
    with TestClient(create_app()) as client:
        info = client.get("/health").json()["backends"]["grok-build"]
    assert info["ready"] is False
    assert info["login"] == "missing"
    assert info["cli_model"] == "grok-4.5"


def test_cli_model_never_carries_probe_text_into_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is one model token, not a line of the probe's output."""
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN", mode="grok_signed_in")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "grok-build")
    with TestClient(create_app()) as client:
        health = client.get("/health")
    assert health.json()["backends"]["grok-build"]["cli_model"] == "grok-4.5"
    assert "You are not authenticated." not in health.text
    assert "Available models" not in health.text


def test_grok_probe_makes_a_signed_in_cli_ready(
    auth_header: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the probe swap: a signed-in `grok` is ready, not stuck false."""
    log = _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN", mode="grok_signed_in")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "grok-build")
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "Reply with exactly: ok"}]},
        )
    info = health["backends"]["grok-build"]
    assert info["implemented"] is True
    assert info["ready"] is True
    assert info["login"] == "ok"
    assert info["probe"] == "models"
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert _calls(log)[0]["argv"][:1] == ["models"]


def test_cli_model_list_parses_only_the_account_list() -> None:
    """Ids come from under a `Models:` header, never from login prose."""
    grok_out = (
        "You are not authenticated.\n\nDefault model: grok-4.5\n\n"
        "Available models:\n  * grok-4.5 (default)\n  grok-4.1-fast\n"
    )
    assert _cli_model_list(grok_out) == ("grok-4.5", "grok-4.1-fast")
    agy_out = (
        "Available models:\n  * gemini-3.8-flash-high (default)\n"
        "  gemini-3.1-pro-high   the balanced one\n"
    )
    assert _cli_model_list(agy_out) == ("gemini-3.8-flash-high", "gemini-3.1-pro-high")
    assert _cli_model_list("Logged in using ChatGPT\n") == ()
    assert _cli_model_list("") == ()
    assert _cli_model_list("models are great\n") == ()
    assert _cli_model_list("Models:\n  user@example.com\n  gpt-5.5\n") == ("gpt-5.5",)
    assert _cli_model_list("Models:\n  a-1\n  a-1\n  b-2\n") == ("a-1", "b-2")


def test_cli_model_slugs_reads_only_a_real_json_listing() -> None:
    """`codex debug models` answers with JSON; prose that says "models" is not."""
    codex_out = json.dumps(
        {
            "models": [
                {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra"},
                {"slug": "gpt-5.5-codex", "display_name": "GPT-5.5-Codex"},
            ]
        }
    )
    assert _cli_model_slugs(codex_out) == ("gpt-6-astra", "gpt-5.5-codex")
    assert _cli_model_slugs("") == ()
    assert _cli_model_slugs("Logged in using ChatGPT\n") == ()
    assert _cli_model_slugs("models are great\n") == ()
    assert _cli_model_slugs('{"models": "gpt-6-astra"}') == ()
    assert _cli_model_slugs('{"models": [{"display_name": "GPT-6-Astra"}]}') == ()
    assert _cli_model_slugs('{"models": [{"slug": "user@example.com"}]}') == ()
    assert _cli_model_slugs('{"models": [{"slug": "[REDACTED:key]"}]}') == ()
    assert _cli_model_slugs('{"models": [{"slug": "a-1"}, {"slug": "a-1"}, {"slug": "b-2"}]}') == (
        "a-1",
        "b-2",
    )


def test_codex_probe_reads_its_own_model_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One probe cycle runs both codex commands, and the list is the account's."""
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_CODEX_BIN", mode="ok")
    with TestClient(create_app()) as client:
        backends = client.get("/health").json()["backends"]
    codex = backends["codex"]
    assert codex["models_source"] == "cli"
    assert codex["models"] == ["gpt-6-astra", "gpt-5.5-codex"]
    assert codex["discovered_models"] == ["gpt-6-astra", "gpt-5.5-codex"]
    assert codex["probe"] == "login status"
    assert codex["login"] == "ok"
    assert codex["models"] != list(CODEX_MODELS)


def test_codex_probe_runs_each_command_once_per_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five /health polls, one `codex debug models` process."""
    log = _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_CODEX_BIN", mode="ok")
    with TestClient(create_app()) as client:
        for _ in range(5):
            assert client.get("/health").json()["backends"]["codex"]["models_source"] == "cli"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    listings = [call["argv"][:2] for call in calls if call["argv"][:2] == ["debug", "models"]]
    assert len(listings) == 1


def test_health_models_are_live_for_grok_and_catalog_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`grok models` and `codex debug models` enumerate; the rest cannot."""
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN", mode="grok_signed_in")
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(tmp_path / "fake-cli"))
    clear_probe_cache()
    with TestClient(create_app()) as client:
        backends = client.get("/health").json()["backends"]
    grok = backends["grok-build"]
    assert grok["models_source"] == "cli"
    assert grok["models"] == ["grok-4.5"]
    assert "grok models" in grok["models_hint"]
    assert grok["discovered_models"] == ["grok-4.5"]

    codex = backends["codex"]
    assert codex["models_source"] == "cli"
    assert codex["models"] == ["gpt-6-astra", "gpt-5.5-codex"]
    assert "codex debug models" in codex["models_hint"]
    assert "no model-listing command" in backends["claude-code"]["models_hint"]
    assert backends["claude-code"]["models"] == list(CLAUDE_CODE_MODELS)
    assert backends["copilot"]["models"] == list(COPILOT_MODELS)

    for name in ("openrouter", "openai", "anthropic", "xai", "nous"):
        row = backends[name]
        assert row["models_source"] == "catalog"
        assert row["default_model"] in row["models"]
        assert "bridge's own catalog" in row["models_hint"]


def test_grok_falls_back_to_catalog_when_the_probe_lists_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery unavailable means the catalog, labelled as the catalog."""
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_GROK_BIN")
    with TestClient(create_app()) as client:
        row = client.get("/health").json()["backends"]["grok-build"]
    assert row["models_source"] == "catalog"
    assert "did not report a model list" in row["models_hint"]
    assert row["models"] == ["grok-build"]

    monkeypatch.setenv("OS3_BRIDGE_AGY_BIN", str(tmp_path / "missing-agy"))
    with TestClient(create_app()) as client:
        agy = client.get("/health").json()["backends"]["antigravity"]
    assert agy["cli_present"] is False
    assert agy["models_source"] == "catalog"
    assert agy["models"] == list(ANTIGRAVITY_MODELS)


def test_v1_models_marks_catalog_rows_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_header: dict[str, str],
) -> None:
    """A catalog row is not an entitlement, so it says so on the row.

    Codex now enumerates its own list, so use claude-code, whose login probe
    cannot list models and therefore still serves the fixed catalog.
    """
    _install_named(tmp_path, monkeypatch, "OS3_BRIDGE_CLAUDE_BIN")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "claude-code")
    with TestClient(create_app()) as client:
        rows = client.get("/v1/models", headers=auth_header).json()["data"]
    ids = [row["id"] for row in rows]
    assert ids[0] == model_for("claude-code")
    assert set(ids) >= set(CLAUDE_CODE_MODELS)
    assert all(str(row.get("warning", "")).startswith("unverified:") for row in rows)
