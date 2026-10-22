"""Secret handling outside the request path: redact, startup, repo hygiene."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from nexus.app import main, startup_lines
from nexus.auth import read_bridge_key
from nexus.backends.registry import active_backend_name
from nexus.redact import redact

ROOT = Path(__file__).resolve().parent.parent


def test_redact_bearer_query_and_known_prefixes() -> None:
    raw = (
        "Authorization: Bearer sk-or-abcdefghijklmnopqrstuvwxyz "
        "api_key=sk-ant-abcdefghijklmnopqrstuvwxyz"
    )
    text = redact(raw, ["os3_explicit_secret_value"])
    assert "sk-or-" not in text
    assert "sk-ant-" not in text
    assert "Bearer [REDACTED]" in text
    assert redact("Missing Bearer token") == "Missing Bearer token"
    assert "api_key=[REDACTED]" in text
    replaced = redact("prefix os3_explicit_secret_value suffix", ["os3_explicit_secret_value"])
    assert "os3_explicit_secret_value" not in replaced
    assert "[REDACTED]" in replaced


def test_startup_and_main_do_not_print_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-or-startup-secret-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.setenv("OS3_BRIDGE_HOST", "localhost")
    text = "\n".join(startup_lines("127.0.0.1", 11435))
    assert secret not in text
    assert '"key_present": true' in text
    with patch("nexus.app.uvicorn.run") as run:
        main()
    run.assert_called_once()
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 11435
    assert run.call_args.kwargs["proxy_headers"] is False
    printed = capsys.readouterr().out
    assert secret not in printed
    minted = read_bridge_key()
    assert minted
    assert minted not in printed


def test_backend_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "")
    assert active_backend_name() == "openrouter"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "Grok")
    assert active_backend_name() == "xai"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "Claude")
    assert active_backend_name() == "anthropic"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "claude-cli")
    assert active_backend_name() == "claude-code"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "claude-code")
    assert active_backend_name() == "claude-code"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "github-copilot")
    assert active_backend_name() == "copilot"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "grok-cli")
    assert active_backend_name() == "grok-build"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "agy")
    assert active_backend_name() == "antigravity"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "gemini")
    assert active_backend_name() == "gemini"
    monkeypatch.delenv("OS3_BRIDGE_BACKEND")
    assert active_backend_name() == "openrouter"


def test_gitignore_and_docs_do_not_embed_keys() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for line in (
        ".os3_bridge_api_key",
        ".os3_bridge_backend",
        ".os3_bridge_model",
        ".nexus_api_key",
        ".nexus_backend",
        ".nexus_model",
        "*.key",
        ".env",
    ):
        assert line in ignore
    banned = ("sk-or-", "sk-ant-", "sk-proj-")
    for relative in (
        "README.md",
        ".env.example",
        "doctor.sh",
        "CONNECT.md",
        "packaging/launchd/ai.nexus.server.plist.template",
        "packaging/systemd/nexus.service",
        "install.sh",
        "os3-bridge",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        for marker in banned:
            assert marker not in text
    allowed_subprocess = {"cli_exec.py", "service.py", "rabbit_watchdog.py", "rabbit_cdp.py"}
    may_read_login_files = {"nous_token.py", "chatgpt_auth.py"}
    for path in (ROOT / "src" / "nexus").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name not in may_read_login_files:
            assert "auth.json" not in text
        assert ".credentials" not in text
        assert "shell=True" not in text
        if path.name not in allowed_subprocess:
            assert "subprocess" not in text
    boundary = (ROOT / "src" / "nexus" / "backends" / "cli_exec.py").read_text(encoding="utf-8")
    assert "subprocess" in boundary
    hermes_boundary = (ROOT / "src" / "nexus" / "nous_token.py").read_text(encoding="utf-8")
    assert "auth.json" in hermes_boundary


def test_concurrent_first_use_agrees_on_one_bridge_key(tmp_path, monkeypatch) -> None:
    """Racing callers on a fresh install must all be handed the key on disk."""
    import threading

    from nexus.auth import ensure_bridge_key

    path = tmp_path / "bridge.key"
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(path))
    for _ in range(40):
        path.unlink(missing_ok=True)
        barrier = threading.Barrier(12)
        seen: list[str] = []

        def first_use(barrier=barrier, seen=seen) -> None:
            barrier.wait()
            seen.append(ensure_bridge_key())

        threads = [threading.Thread(target=first_use) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert {*seen} == {path.read_text(encoding="utf-8").strip()}
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert [p.name for p in tmp_path.iterdir()] == ["bridge.key"]


def test_no_key_reaches_the_logs_or_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    bridge_key: str,
    openrouter_key: str,
) -> None:
    import logging

    import httpx
    from fastapi.testclient import TestClient
    from nexus.app import create_app
    from nexus.backends.openrouter import OpenRouterBackend

    from tests.transport import HandlerTransport

    def provider_echoes_the_key(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"]
        return httpx.Response(401, json={"error": {"message": f"bad credential: {auth}"}})

    caplog.set_level(logging.DEBUG)
    backend = OpenRouterBackend(transport=HandlerTransport(provider_echoes_the_key))
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(create_app(backend=backend)) as client:
        for stream in (False, True):
            client.post(
                "/v1/chat/completions",
                json={**body, "stream": stream},
                headers={"Authorization": f"Bearer {bridge_key}"},
            )
        client.get("/health")
        client.get("/doctor")
    seen = caplog.text + "".join(capsys.readouterr())
    assert "401 Unauthorized" in caplog.text
    assert openrouter_key not in seen
    assert bridge_key not in seen


def test_chatgpt_credentials_never_show_their_token() -> None:
    from nexus.chatgpt_auth import Credentials

    creds = Credentials(access_token="tok-secret-value", account_id="acct-secret", expires_at=1.0)
    assert "tok-secret-value" not in repr(creds) + str(creds)
    assert "acct-secret" not in repr(creds) + str(creds)


def test_upstream_and_devtools_clients_ignore_proxy_env_and_redirects() -> None:
    from nexus.backends.openrouter import OpenRouterBackend
    from nexus.rabbit_cdp import ChromeCdp

    upstream = OpenRouterBackend()._http.client()
    assert upstream.trust_env is False and upstream.follow_redirects is False
    assert ChromeCdp(port=1, profile_dir=Path("/nonexistent"))._client().trust_env is False
