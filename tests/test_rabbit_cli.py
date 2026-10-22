"""./nexus rabbit ...: human output and exit codes over a fake OS page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nexus import cli, rabbit_cli
from nexus.rabbit_cdp import ManualRequired
from nexus.rabbit_setup import RabbitSetup

from tests.rabbit_fakes import (
    BRIDGE_KEY,
    NODE_ID,
    FakeBrowser,
    FakeOs,
    fixture,
    models_source,
)

REAL_ENGINE = RabbitSetup


@pytest.fixture
def os_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeOs:
    home = tmp_path / "home"
    (home / ".rabbit-agent").mkdir(parents=True)
    (home / ".rabbit-agent" / "connection.json").write_text(json.dumps({"nodeId": NODE_ID}))
    monkeypatch.setenv("HOME", str(home))
    key = tmp_path / "bridge.key"
    key.write_text(BRIDGE_KEY + "\n")
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(key))
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    state: dict[str, Any] = {"browser": FakeBrowser(page)}

    def build(**kwargs: Any) -> RabbitSetup:
        return REAL_ENGINE(
            browser=state["browser"], models_source=models_source, retry_delay=0, **kwargs
        )

    monkeypatch.setattr(rabbit_cli, "RabbitSetup", build)
    return page


def run_cli(*argv: str) -> int:
    return cli.main(["rabbit", *argv])


def test_plan_prints_values_without_the_key_and_needs_no_opt_in(
    os_page: FakeOs, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli("plan") == 0
    out = capsys.readouterr().out
    assert "opt-in: OFF" in out
    assert "http://127.0.0.1:11435/v1" in out
    assert "meituan/longcat-2.5-preview:free" in out and "free" in out
    assert "bridge.key" in out and BRIDGE_KEY not in out
    assert "NEXUS_RABBIT_SETUP=on ./nexus rabbit setup --dry-run" in out
    assert os_page.calls == []


def test_setup_without_opt_in_exits_non_zero_and_says_how_to_enable(
    os_page: FakeOs, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli("setup", "--model", "openai/gpt-4o-mini") == 1
    err = capsys.readouterr().err
    assert "opt_in_required" in err and "NEXUS_RABBIT_SETUP=on" in err
    assert os_page.calls == []


def test_dry_run_prints_the_masked_diff(
    os_page: FakeOs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    assert run_cli("setup", "--model", "openai/gpt-4o-mini", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing was changed" in out
    assert "before: custom  https://openrouter.ai/api/v1  anthropic/claude-sonnet-4.5" in out
    assert "after:  local  http://127.0.0.1:11435/v1  openai/gpt-4o-mini  window 65536" in out
    assert "key ****c1d7" in out and BRIDGE_KEY not in out
    assert "may be billed" in out
    assert os_page.paths == ["/api/byok/get"]


def test_setup_verify_and_a_restore_that_cannot_be_done(
    os_page: FakeOs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    assert run_cli("setup", "--model", "openai/gpt-4o-mini") == 0
    assert "Setup done." in capsys.readouterr().out
    assert run_cli("verify") == 0
    assert "OS3 points at this door." in capsys.readouterr().out
    assert run_cli("restore") == 1
    captured = capsys.readouterr()
    assert "restore_failed" in captured.err
    assert BRIDGE_KEY not in captured.out + captured.err


def test_setup_refuses_plainly_when_the_current_model_cannot_be_rolled_back(
    os_page: FakeOs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    os_page.state = fixture("byok_get_local_stealth.json")
    assert run_cli("setup", "--model", "openai/gpt-4o-mini") == 1
    err = capsys.readouterr().err

    assert "rollback_unavailable" in err and "Nothing was changed" in err
    assert "stealth/space-bunny-alpha" in err and "--accept-no-rollback" in err
    assert "Values for a manual setup" in err
    assert "http://127.0.0.1:11435/v1" in err and "openai/gpt-4o-mini" in err
    assert str(tmp_path / "bridge.key") in err and BRIDGE_KEY not in err
    assert os_page.paths == ["/api/byok/get"]

    assert run_cli("setup", "--model", "openai/gpt-4o-mini", "--accept-no-rollback") == 0
    assert "Setup done." in capsys.readouterr().out
    assert os_page.state["config"]["modelId"] == "openai/gpt-4o-mini"


def test_state_unknown_exits_non_zero_and_prints_the_manual_values(
    os_page: FakeOs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    restore_key = tmp_path / "restore.key"
    restore_key.write_text("sk-or-v1-FIXTURE-restore-key-wxyz\n")
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(restore_key))
    os_page.fail_gets_after_delete, os_page.fail_register_times = 1, 1

    assert run_cli("setup", "--model", "openai/gpt-4o-mini") == rabbit_cli.EXIT_FAILED

    err = capsys.readouterr().err
    assert "FAILED (os3_state_unknown): OS3 may have no provider connected" in err
    assert "base URL:" in err and "unchanged" not in err


def test_unknown_model_exits_non_zero_listing_real_ids(
    os_page: FakeOs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    assert run_cli("setup", "--model", "gpt-4o-mni") == 1
    err = capsys.readouterr().err
    assert "unknown_model" in err and "openai/gpt-4o-mini" in err
    assert os_page.calls == []


def test_manual_fallback_exits_with_the_paste_in_values(
    os_page: FakeOs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    os_page.signed_out = True
    assert run_cli("setup", "--model", "openai/gpt-4o-mini") == rabbit_cli.EXIT_MANUAL
    err = capsys.readouterr().err
    assert "https://os3.rabbit.tech/settings/api-keys" in err
    assert "http://127.0.0.1:11435/v1" in err and "openai/gpt-4o-mini" in err
    assert str(tmp_path / "bridge.key") in err and BRIDGE_KEY not in err
    assert "65536" in err


def test_chrome_missing_is_manual_too(
    os_page: FakeOs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    monkeypatch.setattr(
        rabbit_cli,
        "RabbitSetup",
        lambda **kw: REAL_ENGINE(
            browser=FakeBrowser(os_page, unavailable=ManualRequired("chrome_unavailable", "none")),
            models_source=models_source,
            **kw,
        ),
    )
    assert run_cli("setup", "--model", "openai/gpt-4o-mini") == rabbit_cli.EXIT_MANUAL
    assert "chrome_unavailable" in capsys.readouterr().err


def test_parser_accepts_the_documented_commands() -> None:
    parser = cli._parser()
    assert parser.parse_args(["rabbit", "plan"]).rabbit_command == "plan"
    args = parser.parse_args(["rabbit", "setup", "--model", "m", "--dry-run"])
    assert (args.rabbit_command, args.model, args.dry_run) == ("setup", "m", True)
    assert args.accept_no_rollback is False
    assert parser.parse_args(["rabbit", "setup", "--accept-no-rollback"]).accept_no_rollback
    assert parser.parse_args(["rabbit", "restore"]).rabbit_command == "restore"
    assert parser.parse_args(["rabbit", "verify"]).rabbit_command == "verify"
