"""Setup's OS3 CONNECT block names the port the door actually uses."""

from __future__ import annotations

from pathlib import Path

import pytest
from nexus.setup_flow import connect_steps

from tests.test_chatgpt import auth_file  # noqa: F401


def _text() -> str:
    return connect_steps(
        backend="chatgpt",
        model="gpt-6-luna",
        bridge_key=Path("/k/.nexus_api_key"),
        provider_key=None,
    )


def test_connect_uses_the_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_PORT", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_PORT", raising=False)
    assert "Base URL: http://127.0.0.1:11435/v1" in _text()


@pytest.mark.parametrize("env", ["NEXUS_PORT", "OS3_BRIDGE_PORT"])
def test_connect_follows_the_configured_port(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.delenv("NEXUS_PORT", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_PORT", raising=False)
    monkeypatch.setenv(env, "11436")
    text = _text()
    assert "Base URL: http://127.0.0.1:11436/v1" in text
    assert "11435" not in text


def test_setup_command_prints_the_configured_port(
    monkeypatch: pytest.MonkeyPatch,
    auth_file,  # noqa: F811
) -> None:
    from nexus.setup_flow import run_setup

    monkeypatch.setenv("NEXUS_PORT", "11437")
    text = run_setup(backend="chatgpt", api_key=None, non_interactive=True)
    assert "Base URL: http://127.0.0.1:11437/v1" in text
    assert "127.0.0.1:11435" not in text
