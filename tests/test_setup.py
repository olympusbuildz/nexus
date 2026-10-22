"""Setup writes mode-600 key files and prints OS3 CONNECT steps."""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from nexus.backends import cli_exec
from nexus.backends.cli_exec import BREW_BIN_PREFIXES
from nexus.cli import main
from nexus.config import active_backend_id, backend_source, service_backend
from nexus.setup_flow import (
    SetupError,
    offered_backends,
    run_setup,
    write_private_line,
)
from nexus.status import doctor_payload, health_payload

ROOT = Path(__file__).resolve().parent.parent


def test_choice_file_is_replaced_without_a_partial_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader must never see a half-written choice and fall back to the env pin."""
    path = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    path.write_text("nous\n", encoding="utf-8")
    seen: dict[str, object] = {}
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        source = Path(src)
        dest = Path(dst)
        seen["same_dir"] = source.parent == dest.parent
        seen["temp_text"] = source.read_text(encoding="utf-8")
        seen["dest_before"] = dest.read_text(encoding="utf-8")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_private_line(path, "codex", mode=0o644)
    assert seen == {
        "same_dir": True,
        "temp_text": "codex\n",
        "dest_before": "nous\n",
    }
    assert path.read_text(encoding="utf-8") == "codex\n"
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_write_private_line_mode_and_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "openrouter.key"
    write_private_line(path, "sk-or-write-secret-value")
    assert path.read_text(encoding="utf-8") == "sk-or-write-secret-value\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (path.parent / ".openrouter.key.tmp").exists()
    with pytest.raises(SetupError):
        write_private_line(path, "has space")


def test_setup_openrouter_hides_the_key_and_uses_loopback() -> None:
    secret = "sk-or-setup-secret-value"
    text = run_setup(backend="openrouter", api_key=secret, non_interactive=True)
    key_path = Path(os.environ["OS3_BRIDGE_OPENROUTER_KEY_FILE"])
    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    bridge = Path(os.environ["OS3_BRIDGE_KEY_FILE"])
    assert key_path.read_text(encoding="utf-8").strip() == secret
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert choice.read_text(encoding="utf-8").strip() == "openrouter"
    assert bridge.stat().st_mode & 0o777 == 0o600
    assert bridge.read_text(encoding="utf-8").strip()
    assert secret not in text
    assert bridge.read_text(encoding="utf-8").strip() not in text
    assert "OS3 CONNECT" in text
    assert "http://127.0.0.1:11435/v1" in text
    assert "localhost" not in text
    assert "mode 600" in text


def test_setup_keeps_a_model_for_the_same_backend_and_clears_on_change() -> None:
    from nexus.config import model_choice_path

    model_path = model_choice_path()
    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    choice.write_text("openrouter\n", encoding="utf-8")
    model_path.write_text("openai/gpt-4o\n", encoding="utf-8")
    run_setup(backend="openrouter", api_key="sk-or-keep-model-value", non_interactive=True)
    assert model_path.read_text(encoding="utf-8").strip() == "openai/gpt-4o"
    run_setup(backend="openai", api_key="sk-proj-switch-backend", non_interactive=True)
    assert not model_path.exists()


def test_setup_keeps_existing_key_and_tightens_mode() -> None:
    path = Path(os.environ["OS3_BRIDGE_OPENROUTER_KEY_FILE"])
    path.write_text("sk-or-existing-secret-value\n", encoding="utf-8")
    path.chmod(0o644)
    text = run_setup(backend="openrouter", api_key=None, non_interactive=True)
    assert path.read_text(encoding="utf-8").strip() == "sk-or-existing-secret-value"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "sk-or-existing-secret-value" not in text
    assert "Keeping the existing OpenRouter key file" in text


def test_setup_aliases_and_rejects_a_bad_key() -> None:
    text = run_setup(backend="grok", api_key="xai-setup-secret-value", non_interactive=True)
    assert Path(os.environ["OS3_BRIDGE_XAI_KEY_FILE"]).read_text(encoding="utf-8").strip() == (
        "xai-setup-secret-value"
    )
    assert Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).read_text(encoding="utf-8").strip() == "xai"
    assert "xAI API" in text
    assert "xai-setup-secret-value" not in text

    claude = run_setup(
        backend="claude",
        api_key="sk-ant-setup-secret-value",
        non_interactive=True,
    )
    assert "Anthropic API" in claude
    assert "claude-code" in claude
    assert Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).read_text(encoding="utf-8").strip() == (
        "anthropic"
    )
    assert "sk-ant-setup-secret-value" not in claude

    openai_path = Path(os.environ["OS3_BRIDGE_OPENAI_KEY_FILE"])
    with pytest.raises(SetupError):
        run_setup(backend="openai", api_key="has space", non_interactive=True)
    assert not openai_path.exists()
    with pytest.raises(SetupError) as exc:
        run_setup(backend="mlx", api_key="sk-or-not-written-value", non_interactive=True)
    assert "sk-or-not-written-value" not in str(exc.value)


def test_setup_offers_subscription_clis_only_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert offered_backends() == ["openrouter", "openai", "anthropic", "xai", "nous"]
    with pytest.raises(SetupError, match="codex is not on PATH"):
        run_setup(backend="codex", api_key=None, non_interactive=True)
    assert not Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).exists()

    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(binary))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subscription CLI was started")

    monkeypatch.setattr(cli_exec, "run_sync", boom)
    assert "codex" in offered_backends()
    assert "claude-code" not in offered_backends()
    assert "copilot" not in offered_backends()
    assert "grok-build" not in offered_backends()
    assert "antigravity" not in offered_backends()
    text = run_setup(backend="codex", api_key=None, non_interactive=True)
    assert "http://127.0.0.1:11435/v1" in text
    assert "localhost" not in text
    assert "codex login" in text
    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).read_text(encoding="utf-8")
    assert choice.strip() == "codex"
    assert not Path(os.environ["OS3_BRIDGE_OPENROUTER_KEY_FILE"]).exists()
    with pytest.raises(SetupError, match="CLI login"):
        run_setup(backend="codex", api_key="sk-or-should-not-stick", non_interactive=True)


def test_setup_rejects_deprecated_gemini_cli() -> None:
    with pytest.raises(SetupError, match="Unknown backend"):
        run_setup(backend="gemini", api_key=None, non_interactive=True)


def test_gh_on_path_does_not_offer_copilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("OS3_BRIDGE_COPILOT_BIN", raising=False)
    brew_copilot = next(
        (
            prefix / "copilot"
            for prefix in map(Path, BREW_BIN_PREFIXES)
            if (prefix / "copilot").is_file()
        ),
        None,
    )
    if brew_copilot is None:
        pytest.skip("no Homebrew copilot installed on this machine")
    monkeypatch.setenv("OS3_BRIDGE_COPILOT_BIN", str(brew_copilot))
    assert "copilot" in offered_backends()


def test_setup_lists_antigravity_only_when_agy_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_ENABLE_ANTIGRAVITY", "1")
    with pytest.raises(SetupError, match="agy is not on PATH"):
        run_setup(backend="agy", api_key=None, non_interactive=True)
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_BRIDGE_AGY_BIN", str(binary))
    assert "antigravity" in offered_backends()
    text = run_setup(backend="agy", api_key=None, non_interactive=True)
    assert "interactive `agy` session" in text
    assert Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).read_text(encoding="utf-8").strip() == (
        "antigravity"
    )
    assert "The name agy is the Antigravity CLI." in text


def test_setup_refuses_antigravity_until_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_BRIDGE_AGY_BIN", str(binary))
    assert "antigravity" not in offered_backends()
    with pytest.raises(SetupError, match="NEXUS_ENABLE_ANTIGRAVITY=1"):
        run_setup(backend="agy", api_key=None, non_interactive=True)
    assert not Path(os.environ["OS3_BRIDGE_BACKEND_FILE"]).exists()


def test_setup_menu_defaults_to_openrouter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def ask(prompt: str) -> str:
        assert "Choice" in prompt
        return ""

    secret = "sk-or-menu-secret-value"
    text = run_setup(
        backend=None,
        api_key=secret,
        non_interactive=False,
        input_fn=ask,
    )
    menu = capsys.readouterr().out
    assert "OpenRouter API key" in menu
    assert "ChatGPT plan" in menu
    assert "Codex subscription CLI (not installed)" in menu
    assert "Grok Build subscription CLI (not installed)" in menu
    assert "Antigravity subscription CLI (opt-in: set NEXUS_ENABLE_ANTIGRAVITY=1 first)" in menu
    assert "OAuth token from Hermes" not in menu
    assert secret not in menu
    assert secret not in text
    assert "OS3 CONNECT" in text
    with pytest.raises(SetupError):
        run_setup(backend=None, api_key=None, non_interactive=True)


def test_choice_file_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    path.write_text("grok\n", encoding="utf-8")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")
    assert active_backend_id() == "xai"
    assert backend_source() == "file"
    assert service_backend() == "xai"
    monkeypatch.delenv("OS3_BRIDGE_BACKEND")
    assert active_backend_id() == "xai"
    path.write_text("\n", encoding="utf-8")
    assert active_backend_id() == "openrouter"
    assert backend_source() == "default"
    assert service_backend() == "openrouter"


def test_env_is_the_default_when_nothing_is_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    path.unlink(missing_ok=True)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    assert active_backend_id() == "nous"
    assert backend_source() == "env"
    assert service_backend() == "openrouter"


def test_cli_setup_reads_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-or-cli-secret-value"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    assert main(["setup", "--backend", "openrouter", "--api-key-stdin"]) == 0
    out = capsys.readouterr().out
    assert secret not in out
    assert "OS3 CONNECT" in out
    assert "http://127.0.0.1:11435/v1" in out
    assert "localhost" not in out
    written = Path(os.environ["OS3_BRIDGE_OPENROUTER_KEY_FILE"]).read_text(encoding="utf-8")
    assert written.strip() == secret


def test_help_and_docs() -> None:
    assert main([]) == 0
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    connect = (ROOT / "CONNECT.md").read_text(encoding="utf-8")
    head = "\n".join(readme.splitlines()[:80])
    connect_head = "\n".join(connect.splitlines()[:40])
    for text in (head, connect_head):
        assert "./install.sh" in text
        assert "./nexus test" in text
        assert "http://127.0.0.1:11435/v1" in text
        assert "localhost" not in text
    assert "pipx" not in readme.lower()
    assert "pipx" not in connect.lower()
    for name in ("install.sh", "os3-bridge", "nexus", "doctor.sh"):
        path = ROOT / name
        assert os.access(path, os.X_OK)
        parsed = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert parsed.returncode == 0, parsed.stderr


def test_status_does_not_advertise_tools_or_images() -> None:
    for body in (asyncio.run(health_payload()), asyncio.run(doctor_payload())):
        assert "tools" not in body
        assert "images" not in body
        for info in body["backends"].values():
            assert "tools" not in info
            assert "images" not in info
            assert info.get("supports_tools") is None
            assert info.get("supports_images") is None
    assert asyncio.run(doctor_payload())["backend_choice_file"]


def _menu(capsys: pytest.CaptureFixture[str], answer: str) -> tuple[list[str], str | None, str]:
    from nexus.setup_flow import _prompt_backend

    chosen: str | None = None
    error = ""
    try:
        chosen = _prompt_backend(lambda prompt: answer)
    except SetupError as exc:
        error = str(exc)
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()[1:]]
    return lines, chosen, error


def test_menu_order_puts_api_keys_first_and_nous_last(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus.setup_flow import menu_backends

    names = [name for name, _label in menu_backends()]
    assert names[:5] == ["openrouter", "openai", "anthropic", "xai", "chatgpt"]
    assert names[-1] == "nous"
    assert {"codex", "claude-code", "copilot", "grok-build", "antigravity"} <= set(names)
    lines, chosen, _ = _menu(capsys, "")
    assert chosen == "openrouter" and lines[0].startswith("1. OpenRouter")
    assert "advanced" in lines[-1] and "Nous" in lines[-1]


def test_menu_number_picks_chatgpt(capsys: pytest.CaptureFixture[str]) -> None:
    _, chosen, error = _menu(capsys, "5")
    assert (chosen, error) == ("chatgpt", "")


def test_menu_missing_cli_is_listed_but_explains_install(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus.setup_flow import menu_backends

    index = [name for name, _ in menu_backends()].index("grok-build") + 1
    _, chosen, error = _menu(capsys, str(index))
    assert chosen is None and "grok" in error.lower()


def test_menu_antigravity_needs_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus.setup_flow import menu_backends

    index = [name for name, _ in menu_backends()].index("antigravity") + 1
    _, chosen, error = _menu(capsys, str(index))
    assert chosen is None and "NEXUS_ENABLE_ANTIGRAVITY=1" in error


def test_empty_key_says_required() -> None:
    from nexus.setup_flow import _clean_key

    with pytest.raises(SetupError, match="required"):
        _clean_key("   ")
    with pytest.raises(SetupError, match="no spaces"):
        _clean_key("ab cd")


def test_setup_help_lists_chatgpt_and_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus.cli import main

    with pytest.raises(SystemExit):
        main(["setup", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "chatgpt" in out and "grok-build" in out
    assert "antigravity (opt-in" in out and "nous (advanced)" in out
