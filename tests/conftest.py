"""Isolated keys and no real network. Import side effects must not mint secrets."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CREATE_CONNECTION = socket.create_connection


def _refuse_connect(self: socket.socket, address: object) -> None:
    del self
    raise AssertionError(f"real network disabled in tests: {address}")


def _refuse_create_connection(*args: object, **kwargs: object) -> socket.socket:
    del args, kwargs
    raise AssertionError("real network disabled in tests")


@pytest.fixture(autouse=True)
def loopback_test_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Talk to the app the way a real client does: as 127.0.0.1, not "testserver"."""
    original = TestClient.__init__

    def init(self: TestClient, app: object, base_url: str = "http://127.0.0.1:11435", **kw):
        original(self, app, base_url=base_url, **kw)

    monkeypatch.setattr(TestClient, "__init__", init)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "NOUS_API_KEY",
        "OS3_BRIDGE_MODEL",
        "OS3_BRIDGE_MODEL_FILE",
        "OS3_BRIDGE_MODEL_CHAT",
        "OS3_BRIDGE_MODEL_WORKER",
        "OS3_BRIDGE_MODEL_BACKGROUND",
        "OS3_BRIDGE_MODEL_VISION",
        "OS3_BRIDGE_OPENAI_BASE",
        "OS3_BRIDGE_ANTHROPIC_BASE",
        "OS3_BRIDGE_XAI_BASE",
        "OS3_BRIDGE_UPSTREAM_TIMEOUT",
        "OS3_BRIDGE_FALLBACK",
        "OS3_BRIDGE_FALLBACK_COOLDOWN",
        "OS3_BRIDGE_HEALTH_TIMEOUT",
        "OS3_BRIDGE_RABBIT_WATCHDOG",
        "OS3_BRIDGE_RABBIT_AGENT_LABEL",
        "OS3_BRIDGE_RABBIT_WATCHDOG_STATE",
        "OS3_BRIDGE_RABBIT_SETUP",
        "OS3_BRIDGE_RABBIT_PROFILE",
        "OS3_BRIDGE_RABBIT_CDP_PORT",
        "OS3_BRIDGE_RABBIT_CHROME_BIN",
        "OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE",
        "OS3_BRIDGE_RABBIT_KEY_DIRS",
        "OS3_BRIDGE_ENABLE_ANTIGRAVITY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_LABEL", raising=False)
    monkeypatch.setattr("nexus.service.legacy_pin_path", lambda: tmp_path / "legacy-service-pin")
    monkeypatch.setenv("OS3_BRIDGE_OPENROUTER_KEY_FILE", str(tmp_path / "missing-openrouter.key"))
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(tmp_path / "missing-openai.key"))
    monkeypatch.setenv("OS3_BRIDGE_ANTHROPIC_KEY_FILE", str(tmp_path / "missing-anthropic.key"))
    monkeypatch.setenv("OS3_BRIDGE_XAI_KEY_FILE", str(tmp_path / "missing-xai.key"))
    monkeypatch.setenv("OS3_BRIDGE_NOUS_KEY_FILE", str(tmp_path / "missing-nous.key"))
    monkeypatch.setenv("NEXUS_CHATGPT_AUTH_FILE", str(tmp_path / "missing-codex-auth.json"))
    monkeypatch.setenv("NEXUS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(tmp_path / "missing-bridge.key"))
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(tmp_path / "backend-choice"))
    monkeypatch.setenv("OS3_BRIDGE_MODEL_FILE", str(tmp_path / "model-choice"))
    monkeypatch.setenv("OS3_BRIDGE_ROLES_FILE", str(tmp_path / "roles.json"))
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK_FILE", str(tmp_path / "fallback.json"))
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_BACKUP", str(tmp_path / "rabbit-backup.json"))
    monkeypatch.setenv("OS3_UI_DIST", str(tmp_path / "no-ui-dist"))
    monkeypatch.delenv("NEXUS_UI_DIST", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_UI_DIST", raising=False)
    for name in (
        "OS3_BRIDGE_MODEL",
        "OS3_BRIDGE_MODEL_FILE",
        "OS3_BRIDGE_MODEL_CHAT",
        "OS3_BRIDGE_MODEL_WORKER",
        "OS3_BRIDGE_MODEL_BACKGROUND",
        "OS3_BRIDGE_MODEL_VISION",
        "OS3_BRIDGE_OPENAI_BASE",
        "OS3_BRIDGE_ANTHROPIC_BASE",
        "OS3_BRIDGE_XAI_BASE",
        "OS3_BRIDGE_UPSTREAM_TIMEOUT",
        "OS3_BRIDGE_OPENROUTER_TIMEOUT",
        "OS3_BRIDGE_FALLBACK",
        "OS3_BRIDGE_FALLBACK_COOLDOWN",
        "OS3_BRIDGE_HEALTH_TIMEOUT",
        "OS3_BRIDGE_RABBIT_WATCHDOG",
        "OS3_BRIDGE_RABBIT_AGENT_LABEL",
        "OS3_BRIDGE_RABBIT_WATCHDOG_STATE",
        "OS3_BRIDGE_RABBIT_SETUP",
        "OS3_BRIDGE_RABBIT_PROFILE",
        "OS3_BRIDGE_RABBIT_CDP_PORT",
        "OS3_BRIDGE_RABBIT_CHROME_BIN",
        "OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE",
        "OS3_BRIDGE_RABBIT_KEY_DIRS",
        "OS3_BRIDGE_ENABLE_ANTIGRAVITY",
        "OS3_BRIDGE_BACKEND",
        "OS3_BRIDGE_HOST",
        "OS3_BRIDGE_PORT",
        "OS3_BRIDGE_SERVICE",
        "OS3_BRIDGE_TOOL_REPAIR",
        "OS3_BRIDGE_MODEL_FALLBACK",
        "OS3_BRIDGE_MAX_INFLIGHT",
        "OS3_BRIDGE_CODEX_BIN",
        "OS3_BRIDGE_CLAUDE_BIN",
        "OS3_BRIDGE_COPILOT_BIN",
        "OS3_BRIDGE_GROK_BIN",
        "OS3_BRIDGE_AGY_BIN",
        "OS3_BRIDGE_MENUBAR_INTERVAL",
    ):
        monkeypatch.delenv("NEXUS_" + name[len("OS3_BRIDGE_") :], raising=False)
    monkeypatch.setenv("OS3_BRIDGE_MODEL_FALLBACK", "off")
    monkeypatch.setenv("NEXUS_STREAM_USAGE", "0")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openrouter")
    monkeypatch.setenv("OS3_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("OS3_BRIDGE_PORT", "11435")
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(tmp_path / "missing-codex"))
    monkeypatch.setenv("OS3_BRIDGE_CLAUDE_BIN", str(tmp_path / "missing-claude"))
    monkeypatch.setenv("OS3_BRIDGE_COPILOT_BIN", str(tmp_path / "missing-copilot"))
    monkeypatch.setenv("OS3_BRIDGE_GROK_BIN", str(tmp_path / "missing-grok"))
    monkeypatch.setenv("OS3_BRIDGE_AGY_BIN", str(tmp_path / "missing-agy"))
    from nexus import setup_flow
    from nexus.backends.subscription import clear_probe_cache
    from nexus.status import _model_cache

    monkeypatch.setattr(
        setup_flow,
        "verify_provider_key",
        lambda backend, key: setup_flow.VerifyResult(True, "key check stubbed"),
    )
    clear_probe_cache()
    _model_cache.clear()
    monkeypatch.delenv("OS3_BRIDGE_MAX_INFLIGHT", raising=False)
    monkeypatch.setattr(socket.socket, "connect", _refuse_connect)
    monkeypatch.setattr(socket, "create_connection", _refuse_create_connection)


@pytest.fixture
def bridge_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    secret = "os3_test_bridge_key"
    path = tmp_path / "bridge.key"
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(path))
    return secret


@pytest.fixture
def openrouter_key(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "sk-or-test-secret-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    return secret


@pytest.fixture
def auth_header(bridge_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bridge_key}"}
