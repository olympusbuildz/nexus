"""NEXUS_* wins over OS3_BRIDGE_*. Legacy names and choice files still work."""

from __future__ import annotations

from pathlib import Path

import pytest
from nexus.backends.cli_exec import resolve_binary
from nexus.config import (
    active_backend_id,
    backend_choice_from_file,
    backend_choice_path,
    environ_get,
    export_setting,
)


def test_nexus_value_wins_over_the_legacy_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_BACKEND", "openai")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")
    assert environ_get("NEXUS_BACKEND") == "openai"
    assert environ_get("OS3_BRIDGE_BACKEND") == "openai"


def test_empty_nexus_value_does_not_fall_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nexus.config.repo_root", lambda: tmp_path)
    monkeypatch.delenv("NEXUS_BACKEND_FILE", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_BACKEND_FILE", raising=False)
    monkeypatch.setenv("NEXUS_BACKEND", "")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")
    assert environ_get("NEXUS_BACKEND") == ""
    assert environ_get("OS3_BRIDGE_BACKEND") == ""
    assert active_backend_id() == "openrouter"


def test_legacy_name_is_used_when_nexus_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_BACKEND", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "xai")
    assert environ_get("NEXUS_BACKEND") == "xai"
    assert environ_get("OS3_BRIDGE_BACKEND") == "xai"


def test_an_unrelated_variable_is_read_as_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-FAKE-not-a-real-key")
    assert environ_get("OPENROUTER_API_KEY") == "sk-test-FAKE-not-a-real-key"


def test_historical_ui_dist_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_UI_DIST", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_UI_DIST", raising=False)
    monkeypatch.setenv("OS3_UI_DIST", "/tmp/nexus-ui")
    assert environ_get("NEXUS_UI_DIST") == "/tmp/nexus-ui"
    monkeypatch.setenv("NEXUS_UI_DIST", "")
    assert environ_get("OS3_UI_DIST") == ""


def test_cli_bin_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primary = tmp_path / "codex"
    legacy = tmp_path / "legacy-codex"
    for path in (primary, legacy):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("NEXUS_CODEX_BIN", str(primary))
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(legacy))
    assert resolve_binary("NEXUS_CODEX_BIN", "codex") == str(primary)
    assert resolve_binary("OS3_BRIDGE_CODEX_BIN", "codex") == str(primary)
    monkeypatch.delenv("NEXUS_CODEX_BIN")
    assert resolve_binary("OS3_BRIDGE_CODEX_BIN", "codex") == str(legacy)


def test_empty_nexus_cli_bin_does_not_use_the_legacy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "codex"
    legacy.write_text("#!/bin/sh\n", encoding="utf-8")
    legacy.chmod(0o755)
    monkeypatch.setenv("NEXUS_CODEX_BIN", "")
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(legacy))
    assert resolve_binary("NEXUS_CODEX_BIN", "nexus-bin-that-is-not-installed") is None


def test_export_setting_writes_both_spellings() -> None:
    env: dict[str, str] = {}
    export_setting(env, "NEXUS_HOST", "127.0.0.1")
    export_setting(env, "OS3_BRIDGE_PORT", "11435")
    assert env["NEXUS_HOST"] == "127.0.0.1"
    assert env["OS3_BRIDGE_HOST"] == "127.0.0.1"
    assert env["NEXUS_PORT"] == "11435"
    assert env["OS3_BRIDGE_PORT"] == "11435"


def _choice_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.repo_root", lambda: tmp_path)
    monkeypatch.delenv("NEXUS_BACKEND_FILE", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_BACKEND_FILE", raising=False)


def test_legacy_backend_file_is_used_when_the_new_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _choice_root(tmp_path, monkeypatch)
    (tmp_path / ".os3_bridge_backend").write_text("codex\n", encoding="utf-8")
    assert backend_choice_path() == tmp_path / ".os3_bridge_backend"
    assert backend_choice_from_file() == "codex"
    assert active_backend_id() == "codex"


def test_new_backend_file_wins_over_the_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _choice_root(tmp_path, monkeypatch)
    (tmp_path / ".os3_bridge_backend").write_text("codex\n", encoding="utf-8")
    (tmp_path / ".nexus_backend").write_text("openai\n", encoding="utf-8")
    assert backend_choice_path() == tmp_path / ".nexus_backend"
    assert backend_choice_from_file() == "openai"


def test_new_backend_file_is_the_path_when_neither_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _choice_root(tmp_path, monkeypatch)
    assert backend_choice_path() == tmp_path / ".nexus_backend"
    assert backend_choice_from_file() is None


def test_nexus_backend_file_env_wins_over_the_legacy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _choice_root(tmp_path, monkeypatch)
    chosen = tmp_path / "chosen"
    ignored = tmp_path / "ignored"
    chosen.write_text("xai\n", encoding="utf-8")
    ignored.write_text("codex\n", encoding="utf-8")
    (tmp_path / ".os3_bridge_backend").write_text("openrouter\n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_BACKEND_FILE", str(chosen))
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(ignored))
    assert backend_choice_path() == chosen
    assert backend_choice_from_file() == "xai"


def test_legacy_backend_file_env_is_used_when_nexus_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _choice_root(tmp_path, monkeypatch)
    chosen = tmp_path / "chosen"
    chosen.write_text("nous\n", encoding="utf-8")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(chosen))
    assert backend_choice_path() == chosen
    assert backend_choice_from_file() == "nous"
