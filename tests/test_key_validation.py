"""Setup proves a key before it trusts one; doctor fails on a dead credential.

The shipped default wrote a placeholder into the OpenRouter key file and
reported success; the first real chat then got an upstream 401. These tests
pin the two fixes: a placeholder or upstream-refused key can no longer produce
a success report, and doctor.sh exits non-zero in that state.
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import threading
import types
from pathlib import Path

import httpx
import nexus.setup_flow as setup_flow
import pytest
from nexus.setup_flow import (
    SetupError,
    VerifyResult,
    placeholder_reason,
    provider_credential_report,
    run_setup,
)

ROOT = Path(__file__).resolve().parent.parent

REAL_VERIFY = setup_flow.verify_provider_key


def _key_file() -> Path:
    return Path(os.environ["OS3_BRIDGE_OPENROUTER_KEY_FILE"])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_placeholder_reason_flags_shapes_and_passes_real_keys() -> None:
    assert placeholder_reason("") is not None
    assert placeholder_reason("sk-or-v1-short") is not None
    assert placeholder_reason("sk-or-v1-dum" + "a" * 40) is not None
    assert placeholder_reason("your-api-key-here-123456") is not None
    assert placeholder_reason("changeme-123456789012345") is not None
    assert placeholder_reason("sk-or-v1-" + "a" * 48) is None
    assert placeholder_reason("sk-proj-dashboard-test-key") is None


def test_setup_rejects_a_placeholder_key_without_writing() -> None:
    key_path = _key_file()
    with pytest.raises(SetupError, match="placeholder"):
        run_setup(
            backend="openrouter",
            api_key="sk-or-v1-dummy-placeholder-value",
            non_interactive=True,
        )
    assert not key_path.exists()
    assert not Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).exists()


def test_setup_rejects_an_absurdly_short_key() -> None:
    with pytest.raises(SetupError, match="too short"):
        run_setup(backend="openrouter", api_key="sk-or-v1-short", non_interactive=True)
    assert not _key_file().exists()


def test_setup_fails_when_the_upstream_rejects_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = VerifyResult(False, "OpenRouter rejected the key (HTTP 401)")
    monkeypatch.setattr(setup_flow, "verify_provider_key", lambda backend, key: rejected)
    key_path = _key_file()
    with pytest.raises(SetupError, match="rejected"):
        run_setup(
            backend="openrouter",
            api_key="sk-or-v1-" + "a" * 48,
            non_interactive=True,
        )
    assert not key_path.exists()
    assert not Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).exists()


def test_setup_does_not_report_success_when_the_key_cannot_be_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = VerifyResult(None, "could not reach OpenRouter to check the key (ConnectError)")
    monkeypatch.setattr(setup_flow, "verify_provider_key", lambda backend, key: unknown)
    with pytest.raises(SetupError, match="could not be verified"):
        run_setup(
            backend="openrouter",
            api_key="sk-or-v1-" + "a" * 48,
            non_interactive=True,
        )
    assert not _key_file().exists()


def test_setup_refuses_to_keep_a_placeholder_key_file() -> None:
    key_path = _key_file()
    key_path.write_text("sk-or-v1-dum-existing-placeholder\n", encoding="utf-8")
    with pytest.raises(SetupError, match="placeholder"):
        run_setup(backend="openrouter", api_key=None, non_interactive=True)


def test_setup_refuses_to_keep_a_key_the_upstream_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = _key_file()
    key_path.write_text("sk-or-v1-" + "a" * 48 + "\n", encoding="utf-8")
    rejected = VerifyResult(False, "OpenRouter rejected the key (HTTP 401)")
    monkeypatch.setattr(setup_flow, "verify_provider_key", lambda backend, key: rejected)
    with pytest.raises(SetupError, match="rejected"):
        run_setup(backend="openrouter", api_key=None, non_interactive=True)


def test_subscription_backends_skip_key_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(binary))

    def boom(backend: str, key: str) -> VerifyResult:
        raise AssertionError("subscription backends have no key to verify")

    monkeypatch.setattr(setup_flow, "verify_provider_key", boom)
    text = run_setup(backend="codex", api_key=None, non_interactive=True)
    assert "codex login" in text
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    code, lines = provider_credential_report()
    assert code == 0
    assert "SKIP" in lines[0]


def test_credential_report_fails_on_a_rejected_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-or-v1-" + "a" * 48
    _key_file().write_text(secret + "\n", encoding="utf-8")
    rejected = VerifyResult(False, "OpenRouter rejected the key (HTTP 401)")
    monkeypatch.setattr(setup_flow, "verify_provider_key", lambda backend, key: rejected)
    code, lines = provider_credential_report()
    assert code != 0
    assert "FAIL" in lines[0]
    assert secret not in " ".join(lines)


def test_credential_report_fails_on_a_placeholder_without_network() -> None:
    _key_file().write_text("sk-or-v1-dum-shipped-placeholder\n", encoding="utf-8")
    code, lines = provider_credential_report()
    assert code != 0
    assert "FAIL" in lines[0]
    assert "placeholder" in lines[0]


def test_credential_report_skips_a_missing_key() -> None:
    code, lines = provider_credential_report()
    assert code == 0
    assert "SKIP" in lines[0]


def test_verify_provider_key_maps_the_upstream_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, str]]] = []
    state = {"status": 401}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
            sent.append((url, headers or {}))
            return httpx.Response(state["status"], json={})

    fake_httpx = types.SimpleNamespace(
        Client=FakeClient, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError
    )
    monkeypatch.setattr(setup_flow, "httpx", fake_httpx)

    verdict = REAL_VERIFY("openrouter", "sk-or-v1-" + "a" * 48)
    assert verdict.accepted is False
    assert sent[-1][0].endswith("/api/v1/key")
    assert sent[-1][1]["Authorization"].startswith("Bearer ")

    state["status"] = 200
    assert REAL_VERIFY("openai", "sk-proj-" + "b" * 40).accepted is True
    assert sent[-1][0].endswith("/v1/models")

    state["status"] = 403
    verdict = REAL_VERIFY("anthropic", "sk-ant-" + "c" * 40)
    assert verdict.accepted is False
    assert sent[-1][1]["x-api-key"].startswith("sk-ant-")

    state["status"] = 500
    assert REAL_VERIFY("xai", "xai-" + "d" * 40).accepted is None


class _RejectAll(http.server.BaseHTTPRequestHandler):
    """A stand-in upstream that refuses every credential it is handed."""

    def do_GET(self) -> None:
        body = b'{"error":{"message":"User not found","code":401}}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _doctor_env(tmp_path: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env["OS3_BRIDGE_KEY_FILE"] = str(tmp_path / "bridge.key")
    env["OS3_BRIDGE_OPENROUTER_KEY_FILE"] = str(tmp_path / "openrouter.key")
    env["OS3_BRIDGE_BACKEND_FILE"] = str(tmp_path / "backend-choice")
    env["OS3_BRIDGE_BACKEND"] = "openrouter"
    env["OS3_BRIDGE_DOCTOR_SKIP_SMOKE"] = "1"
    env["OS3_BRIDGE_DOCTOR_HEALTH_ATTEMPTS"] = "1"
    env["OS3_BRIDGE_PORT"] = str(port)
    for name in ("CODEX", "CLAUDE", "COPILOT", "GROK", "AGY"):
        env[f"OS3_BRIDGE_{name}_BIN"] = str(tmp_path / f"missing-{name.lower()}")
    return env


def _run_doctor(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "doctor.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


def test_doctor_sh_fails_loudly_when_the_upstream_rejects_the_key(tmp_path: Path) -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), _RejectAll)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        upstream_port = server.server_address[1]
        secret = "sk-or-v1-" + "a" * 48
        (tmp_path / "openrouter.key").write_text(secret + "\n", encoding="utf-8")
        env = _doctor_env(tmp_path, _free_port())
        env["OS3_BRIDGE_OPENROUTER_BASE"] = f"http://127.0.0.1:{upstream_port}/api/v1"
        result = _run_doctor(env)
    finally:
        server.shutdown()
        thread.join()
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "credential: FAIL" in combined
    assert "rejected" in combined
    assert secret not in combined


def test_doctor_sh_fails_loudly_on_a_placeholder_key(tmp_path: Path) -> None:
    (tmp_path / "openrouter.key").write_text("sk-or-v1-dum-shipped-placeholder\n", encoding="utf-8")
    result = _run_doctor(_doctor_env(tmp_path, _free_port()))
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "credential: FAIL" in combined
    assert "sk-or-v1-dum" not in combined
