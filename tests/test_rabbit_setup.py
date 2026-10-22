"""Rabbit OS3 setup engine against a fake OS page. No Chrome, no Rabbit, no network."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from nexus.rabbit_cdp import ManualRequired, PageError
from nexus.rabbit_setup import RabbitError, RabbitSetup, credential

from tests.rabbit_fakes import (
    BRIDGE_KEY,
    ENDPOINT,
    NODE_ID,
    RESTORE_KEY,
    STEALTH_MODEL,
    FakeBrowser,
    FakeOs,
    fixture,
    models_source,
)


@pytest.fixture(autouse=True)
def rabbit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".rabbit-agent").mkdir(parents=True)
    (home / ".rabbit-agent" / "connection.json").write_text(json.dumps({"nodeId": NODE_ID}))
    monkeypatch.setenv("HOME", str(home))
    key = tmp_path / "bridge.key"
    key.write_text(BRIDGE_KEY + "\n", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(key))
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    return tmp_path


def engine(os_page: FakeOs | None = None, **kwargs: Any) -> tuple[RabbitSetup, FakeBrowser, FakeOs]:
    page = os_page or FakeOs()
    browser = FakeBrowser(page, unavailable=kwargs.pop("unavailable", None))
    return (
        RabbitSetup(browser=browser, models_source=models_source, retry_delay=0, **kwargs),
        browser,
        page,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def backup_text(tmp_path: Path) -> str:
    return (tmp_path / "rabbit-backup.json").read_text(encoding="utf-8")


def test_dry_run_diff_masks_key_and_changes_nothing(rabbit_env: Path) -> None:
    rabbit, browser, page = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    result = run(rabbit.apply(model="openai/gpt-4o-mini", dry_run=True))

    assert result["ok"] and result["dry_run"] and result["changed"] is False
    assert result["diff"] == {
        "before": {
            "provider": "custom",
            "endpoint": "https://openrouter.ai/api/v1",
            "modelId": "anthropic/claude-sonnet-4.5",
            "masked": "sk-or-v1-****wxyz",
        },
        "after": {
            "provider": "local",
            "endpoint": ENDPOINT,
            "modelId": "openai/gpt-4o-mini",
            "contextWindow": 65536,
            "key": "****c1d7",
        },
    }
    assert BRIDGE_KEY not in json.dumps(result)
    assert page.paths == ["/api/byok/get"]
    assert not (rabbit_env / "rabbit-backup.json").exists()
    assert any("custom" in w for w in result["warnings"])


def test_dry_run_with_no_current_connection(rabbit_env: Path) -> None:
    rabbit, _, _ = engine(FakeOs(fixture("byok_get_none.json")))
    result = run(rabbit.apply(model="openai/gpt-4o-mini", dry_run=True))
    assert result["ok"] and result["diff"]["before"] is None


def test_apply_replaces_deletes_registers_and_verifies(rabbit_env: Path) -> None:
    rabbit, browser, page = engine(FakeOs(fixture("byok_get_local_other.json")))
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] and result["changed"] and result["mode"] == "auto"
    assert page.paths == [
        "/api/byok/get",
        "/api/byok/delete",
        "/api/byok/get",
        "/api/byok/register",
        "/api/byok/get",
    ]
    assert page.calls[1][1] == {"provider": "local", "feature": "os3", "key": "local"}
    assert page.calls[3][1] == {
        "provider": "local",
        "feature": "os3",
        "nodeId": NODE_ID,
        "endpoint": ENDPOINT,
        "modelId": "openai/gpt-4o-mini",
        "contextWindow": 65536,
    }
    assert page.registered_keys == [BRIDGE_KEY]
    assert result["verified"]["modelId"] == "openai/gpt-4o-mini"
    assert browser.opened == browser.closed == 1


def test_backup_is_private_and_holds_no_key(rabbit_env: Path) -> None:
    rabbit, _, _ = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    run(rabbit.apply(model="openai/gpt-4o-mini"))

    path = rabbit_env / "rabbit-backup.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["before"]["provider"] == "custom"
    assert saved["before"]["masked"] == "sk-or-v1-****wxyz"
    assert saved["applied"]["model"] == "openai/gpt-4o-mini"
    assert BRIDGE_KEY not in path.read_text()


def test_key_never_appears_in_results_logs_or_files(
    rabbit_env: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    rabbit, _, page = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    results = [
        run(rabbit.plan("openai/gpt-4o-mini")),
        run(rabbit.status()),
        run(rabbit.apply(model="openai/gpt-4o-mini", dry_run=True)),
        run(rabbit.apply(model="openai/gpt-4o-mini")),
        run(rabbit.verify()),
    ]
    page.fail_register_times = 1
    page.state = None
    results.append(run(rabbit.apply(model="anthropic/claude-sonnet-4.5")))
    page.signed_out = True
    results.append(run(rabbit.apply(model="openai/gpt-4o-mini")))

    seen = json.dumps(results) + caplog.text + capsys.readouterr().out + backup_text(rabbit_env)
    assert BRIDGE_KEY not in seen
    assert page.registered_keys


def test_reapply_keeps_the_original_backup(rabbit_env: Path) -> None:
    rabbit, _, _ = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    run(rabbit.apply(model="openai/gpt-4o-mini"))
    second = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    assert second["backup"]["kept_existing"] is True
    saved = json.loads(backup_text(rabbit_env))
    assert saved["before"]["provider"] == "custom"
    assert saved["applied"]["model"] == "anthropic/claude-sonnet-4.5"


def test_already_configured_is_a_no_op(rabbit_env: Path) -> None:
    initial = fixture("byok_get_local_nexus_toplevel.json")
    rabbit, _, page = engine(FakeOs(initial))
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] and result["changed"] is False
    assert page.paths == ["/api/byok/get"]


def test_top_level_credential_shape_is_read() -> None:
    cred = credential(fixture("byok_get_local_nexus_toplevel.json"))
    assert cred is not None and cred["provider"] == "local"
    assert cred["config"]["modelId"] == "openai/gpt-4o-mini"
    assert credential(fixture("byok_get_none.json")) is None


def test_top_level_shape_from_the_server_works_end_to_end(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_local_other.json"))
    page.nested = False
    rabbit, _, _ = engine(page)
    assert run(rabbit.apply(model="openai/gpt-4o-mini"))["ok"]


def test_unknown_model_is_refused_before_anything_is_touched(rabbit_env: Path) -> None:
    rabbit, browser, page = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    with pytest.raises(RabbitError) as raised:
        run(rabbit.apply(model="openai/gpt-4o-mni"))

    assert raised.value.code == "unknown_model"
    assert "openai/gpt-4o-mini" in raised.value.hint
    assert raised.value.as_dict()["closest"][0] == "openai/gpt-4o-mini"
    assert browser.opened == 0 and page.calls == []
    assert not (rabbit_env / "rabbit-backup.json").exists()


def test_model_id_is_canonicalized_by_case_only(rabbit_env: Path) -> None:
    rabbit, _, page = engine(FakeOs())
    result = run(rabbit.apply(model="google/gemma-4-31b-it:free"))
    assert result["model"] == "Google/Gemma-4-31B-it:free"
    assert page.calls[-2][1]["modelId"] == "Google/Gemma-4-31B-it:free"
    with pytest.raises(RabbitError):
        run(rabbit.apply(model="gemma-4-31b-it:free", dry_run=True))


def test_default_model_prefers_the_active_one(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL", "anthropic/claude-sonnet-4.5")
    rabbit, _, _ = engine()
    plan = run(rabbit.plan())
    assert (plan["model"], plan["model_source"]) == ("anthropic/claude-sonnet-4.5", "active")


def test_default_model_falls_back_to_longcat(rabbit_env: Path) -> None:
    rabbit, _, _ = engine()
    plan = run(rabbit.plan())
    assert (plan["model"], plan["model_source"]) == (
        "meituan/longcat-2.5-preview:free",
        "fallback",
    )
    assert plan["billing"] == "free" and plan["warnings"] == []


def test_active_model_missing_from_the_list_is_a_problem(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL", "not/a-model")
    rabbit, _, _ = engine()
    plan = run(rabbit.plan())
    assert plan["ok"] is False and plan["problems"][0]["code"] == "unknown_model"


def test_models_list_failure_refuses_apply(rabbit_env: Path) -> None:
    async def broken() -> list[dict[str, Any]]:
        raise OSError("down")

    page = FakeOs()
    rabbit = RabbitSetup(browser=FakeBrowser(page), models_source=broken, retry_delay=0)
    with pytest.raises(RabbitError) as raised:
        run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert raised.value.code == "models_unavailable"
    assert page.calls == []


def test_missing_bridge_key_refuses(rabbit_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(rabbit_env / "nope.key"))
    rabbit, browser, _ = engine()
    with pytest.raises(RabbitError) as raised:
        run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert raised.value.code == "bridge_key_missing" and browser.opened == 0


def test_billed_model_warns_in_plan_and_apply(rabbit_env: Path) -> None:
    rabbit, _, _ = engine()
    plan = run(rabbit.plan("openai/gpt-4o-mini"))
    assert plan["billing"] == "unknown"
    assert any("may be billed" in w for w in plan["warnings"])
    result = run(rabbit.apply(model="openai/gpt-4o-mini", dry_run=True))
    assert any("may be billed" in w for w in result["warnings"])
    assert result["billing"] == "unknown"


def test_free_model_has_no_billing_warning(rabbit_env: Path) -> None:
    rabbit, _, _ = engine()
    result = run(rabbit.apply(model="meituan/longcat-2.5-preview:free", dry_run=True))
    assert result["billing"] == "free"
    assert not any("billed" in w for w in result["warnings"])


def test_plan_lists_models_with_billing(rabbit_env: Path) -> None:
    rabbit, _, _ = engine()
    rows = {row["id"]: row["billing"] for row in run(rabbit.plan())["models"]}
    assert rows["meituan/longcat-2.5-preview:free"] == "free"
    assert rows["openai/gpt-4o-mini"] == "unknown"


def test_every_write_is_refused_while_opt_in_is_off(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OS3_BRIDGE_RABBIT_SETUP")
    rabbit, browser, page = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    for call in (
        lambda: rabbit.apply(model="openai/gpt-4o-mini"),
        lambda: rabbit.apply(model="openai/gpt-4o-mini", dry_run=True),
        lambda: rabbit.restore(),
        lambda: rabbit.verify(),
    ):
        with pytest.raises(RabbitError) as raised:
            run(call())
        assert raised.value.code == "opt_in_required" and raised.value.status == 403
        assert "NEXUS_RABBIT_SETUP=on" in raised.value.hint
    assert browser.opened == 0 and page.calls == []

    plan, status = run(rabbit.plan()), run(rabbit.status())
    assert plan["opt_in"] is False and status["opt_in"] is False


@pytest.mark.parametrize("code", ["signed_out", "blocked", "chrome_unavailable", "cdp_unavailable"])
def test_unreachable_browser_returns_manual_values(rabbit_env: Path, code: str) -> None:
    reason = ManualRequired(code, f"reason for {code}")
    rabbit, _, page = engine(unavailable=reason)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is False and result["mode"] == "manual" and result["code"] == code
    assert result["manual"] == {
        "base_url": ENDPOINT,
        "key_file": str(rabbit_env / "bridge.key"),
        "model_id": "openai/gpt-4o-mini",
        "context_window": 65536,
    }
    assert result["url"] == "https://os3.rabbit.tech/settings/api-keys"
    assert BRIDGE_KEY not in json.dumps(result)
    assert page.calls == []


@pytest.mark.parametrize("flag", ["signed_out", "blocked"])
def test_session_failure_on_the_page_is_manual_and_deletes_nothing(
    rabbit_env: Path, flag: str
) -> None:
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    setattr(page, flag, True)
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert result["mode"] == "manual" and result["code"] == flag
    assert page.state is not None and "/api/byok/delete" not in page.paths
    assert not (rabbit_env / "rabbit-backup.json").exists()


def test_missing_node_id_is_manual(rabbit_env: Path) -> None:
    (rabbit_env / "home" / ".rabbit-agent" / "connection.json").unlink()
    rabbit, browser, _ = engine()
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert result["mode"] == "manual" and result["code"] == "rabbit_agent_missing"
    assert browser.opened == 0


def test_register_failure_after_delete_restores_the_old_local_connection(
    rabbit_env: Path,
) -> None:
    page = FakeOs(fixture("byok_get_local_nexus_toplevel.json"))
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    assert result["ok"] is False and result["code"] == "restore_attempted"
    assert result["restore"]["outcome"] == "restored"
    assert result["cause"]["code"] == "register_failed"
    assert "put back" in result["error"]
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"
    assert page.registered_keys == [BRIDGE_KEY, BRIDGE_KEY]


def test_unrestorable_custom_provider_is_reported_loudly(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is False and result["code"] == "restore_failed"
    assert "could NOT be put back" in result["error"]
    assert "NEXUS_RABBIT_RESTORE_KEY_FILE" in result["restore"]["detail"]
    assert page.state is None
    assert result["hint"].startswith("OS3 may have no provider")


def _custom_fixture() -> dict[str, Any]:
    return fixture("byok_get_custom_openrouter.json")


def test_custom_provider_is_restored_from_a_matching_key_file(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    page = FakeOs(_custom_fixture())
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "restore_attempted"
    assert page.state["provider"] == "custom"
    assert page.state["tierModels"] == _custom_fixture()["credential"]["tierModels"]
    assert page.registered_keys[-1] == RESTORE_KEY
    assert RESTORE_KEY not in json.dumps(result)


def test_custom_restore_uses_the_real_register_and_one_update_per_tier(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    page = FakeOs(_custom_fixture())
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "restore_attempted"
    assert not [w for w in result["warnings"] if w.startswith("tiers_not_restored")]
    register = [b for p, b in page.calls if p == "/api/byok/register"][-1]
    assert register == {
        "provider": "custom",
        "feature": "os3",
        "endpoint": "https://openrouter.ai/api/v1",
    }
    updates = [b for p, b in page.calls if p == "/api/byok/update-model"]
    assert updates == [
        {"feature": "os3", "tier": "small", "model": "anthropic/claude-haiku-4.5"},
        {"feature": "os3", "tier": "standard", "model": "anthropic/claude-sonnet-4.5"},
    ]
    assert page.state["tierModels"] == _custom_fixture()["credential"]["tierModels"]


def test_update_model_is_not_called_for_a_local_backup(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_local_nexus_toplevel.json"))
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))
    assert result["code"] == "restore_attempted"
    assert "/api/byok/update-model" not in page.paths


def test_restore_command_puts_back_a_custom_provider_with_tiers(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    rabbit, _, page = engine(FakeOs(_custom_fixture()))
    assert run(rabbit.apply(model="openai/gpt-4o-mini"))["ok"]

    result = run(rabbit.restore())

    assert result["ok"] is True and result["changed"] is True and result["warnings"] == []
    assert page.state["provider"] == "custom"
    assert page.state["tierModels"] == _custom_fixture()["credential"]["tierModels"]
    assert RESTORE_KEY not in json.dumps(result)


def test_tier_update_failure_is_a_warning_not_a_failed_restore(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    rabbit, _, page = engine(FakeOs(_custom_fixture()))
    assert run(rabbit.apply(model="openai/gpt-4o-mini"))["ok"]
    page.update_model_status = 500

    result = run(rabbit.restore())

    assert result["ok"] is True and result["changed"] is True
    assert "code" not in result
    (warning,) = result["warnings"]
    assert warning.startswith("tiers_not_restored")
    assert "small=anthropic/claude-haiku-4.5" in warning
    assert "standard=anthropic/claude-sonnet-4.5" in warning
    assert page.state["provider"] == "custom"
    last_register = len(page.paths) - 1 - page.paths[::-1].index("/api/byok/register")
    after_register = page.paths[last_register:]
    assert "/api/byok/delete" not in after_register
    assert json.loads(backup_text(rabbit_env))["applied"] is None


def test_tiers_that_read_back_differently_are_a_warning(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    page = FakeOs(_custom_fixture())
    original = page.call

    async def drops_standard(path: str, body: dict[str, Any]):
        reply = await original(path, body)
        if path == "/api/byok/update-model" and body["tier"] == "standard":
            page.state["tierModels"].pop("standard")
        return reply

    page.call = drops_standard  # type: ignore[method-assign]
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "restore_attempted" and result["restore"]["outcome"] == "restored"
    (warning,) = [w for w in result["warnings"] if w.startswith("tiers_not_restored")]
    assert "standard=anthropic/claude-sonnet-4.5" in warning and "small=" not in warning


def test_custom_backup_without_an_endpoint_is_refused_before_any_delete(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    data = _custom_fixture()
    data["credential"]["config"]["endpoint"] = None
    rabbit, _, page = engine(FakeOs(data))
    assert run(rabbit.apply(model="openai/gpt-4o-mini"))["ok"]
    deletes = page.paths.count("/api/byok/delete")

    result = run(rabbit.restore())

    assert result["code"] == "restore_failed" and result["changed"] is False
    assert page.paths.count("/api/byok/delete") == deletes


def test_restore_key_file_that_does_not_match_is_not_used(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text("sk-or-v1-some-other-key-0000\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert result["code"] == "restore_failed" and "does not match" in result["restore"]["detail"]


def test_failed_delete_leaves_the_old_connection_and_does_not_register(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_local_nexus_toplevel.json"))
    page.delete_status = 500
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    assert result["ok"] is False and result["code"] == "delete_failed"
    assert result["restore"]["outcome"] == "unchanged"
    assert "unchanged" in result["error"]
    assert "/api/byok/register" not in page.paths


def test_verify_mismatch_after_register_is_reported(rabbit_env: Path) -> None:
    page = FakeOs()
    original = page.register

    async def wrong_model(body: dict[str, Any], key: str):
        reply = await original(body, key)
        page.state["config"]["modelId"] = "something/else"
        return reply

    page.register = wrong_model  # type: ignore[method-assign]
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert result["ok"] is False and result["code"] == "verify_failed"


def test_restore_puts_back_the_saved_local_connection(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_local_nexus_toplevel.json"))
    rabbit, _, _ = engine(page)
    run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))
    assert page.state["config"]["modelId"] == "anthropic/claude-sonnet-4.5"

    result = run(rabbit.restore())
    assert result["ok"] and result["changed"]
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"
    assert json.loads(backup_text(rabbit_env))["applied"] is None
    assert BRIDGE_KEY not in json.dumps(result)


def test_restore_of_an_unrestorable_provider_changes_nothing(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    rabbit, _, _ = engine(page)
    run(rabbit.apply(model="openai/gpt-4o-mini"))
    result = run(rabbit.restore())

    assert result["ok"] is False and result["code"] == "restore_failed"
    assert result["changed"] is False
    assert page.state["provider"] == "local"
    assert page.paths.count("/api/byok/delete") == 1


def test_restore_to_no_connection_removes_only_ours(rabbit_env: Path) -> None:
    page = FakeOs()
    rabbit, _, _ = engine(page)
    run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert run(rabbit.restore())["ok"] and page.state is None


def test_restore_without_a_backup_is_refused(rabbit_env: Path) -> None:
    rabbit, browser, _ = engine()
    with pytest.raises(RabbitError) as raised:
        run(rabbit.restore())
    assert raised.value.code == "no_backup" and browser.opened == 0


def test_verify_reports_checks_status_and_traffic(rabbit_env: Path) -> None:
    page = FakeOs()
    entries: list[dict[str, Any]] = []

    async def pulse() -> list[dict[str, Any]]:
        return entries

    rabbit, _, _ = engine(page, pulse_source=pulse)
    run(rabbit.apply(model="openai/gpt-4o-mini"))
    page.state["status"] = {"lastCallOutcome": "ok", "lastCallAt": "2026-10-08T10:00:00Z"}

    quiet = run(rabbit.verify())
    assert quiet["ok"] and quiet["checks"] == {
        "provider_local": True,
        "endpoint_matches": True,
        "model_matches_applied": True,
        "model_in_live_list": True,
    }
    assert quiet["call_status"]["lastCallOutcome"] == "ok"
    assert quiet["nexus_saw_request"]["v1"] is False

    entries.append({"ts": "2999-01-01T00:00:00Z", "route": "POST /v1/chat/completions"})
    busy = run(rabbit.verify())
    assert busy["nexus_saw_request"] == {
        "since": busy["applied_at"],
        "v1": True,
        "chat": True,
    }


def test_verify_fails_when_os3_points_elsewhere(rabbit_env: Path) -> None:
    rabbit, _, _ = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    result = run(rabbit.verify())
    assert result["ok"] is False and result["code"] == "verify_failed"
    assert result["checks"]["provider_local"] is False


def test_verify_model_not_in_live_list(rabbit_env: Path) -> None:
    page = FakeOs()
    rabbit, _, _ = engine(page)
    run(rabbit.apply(model="openai/gpt-4o-mini"))
    page.state["config"]["modelId"] = "gone/model"
    result = run(rabbit.verify())
    assert result["checks"]["model_in_live_list"] is False and result["ok"] is False


def test_status_reads_only_local_state(rabbit_env: Path) -> None:
    rabbit, browser, page = engine()
    status = run(rabbit.status())
    assert status["opt_in"] is True
    assert status["node_id"]["present"] and status["key"]["present"]
    assert status["backup"]["present"] is False and status["last_apply"] is None
    assert status["cdp"]["listening"] is True
    assert browser.opened == 0 and page.calls == []
    assert os.fspath(rabbit_env / "bridge.key") in json.dumps(status)


def stealth_page() -> FakeOs:
    return FakeOs(fixture("byok_get_local_stealth.json"))


def test_the_fake_os_refuses_a_model_the_endpoint_does_not_list() -> None:
    page = stealth_page()
    reply = run(
        page.register(
            {"provider": "local", "endpoint": ENDPOINT, "modelId": STEALTH_MODEL}, BRIDGE_KEY
        )
    )
    assert reply.status == 400
    ok = run(
        page.register(
            {"provider": "local", "endpoint": ENDPOINT, "modelId": "openai/gpt-4o-mini"},
            BRIDGE_KEY,
        )
    )
    assert ok.ok


def test_apply_refuses_before_any_delete_when_the_current_model_is_not_live(
    rabbit_env: Path,
) -> None:
    rabbit, _, page = engine(stealth_page())
    with pytest.raises(RabbitError) as raised:
        run(rabbit.apply(model="openai/gpt-4o-mini"))

    err = raised.value
    assert (err.status, err.code) == (409, "rollback_unavailable")
    assert STEALTH_MODEL in err.message and "accept_no_rollback" in err.hint
    body = err.as_dict()
    assert body["manual"] == {
        "base_url": ENDPOINT,
        "key_file": str(rabbit_env / "bridge.key"),
        "model_id": "openai/gpt-4o-mini",
        "context_window": 65536,
    }
    assert body["url"] == "https://os3.rabbit.tech/settings/api-keys"
    assert BRIDGE_KEY not in json.dumps(body)
    assert page.paths == ["/api/byok/get"]
    assert page.state["config"]["modelId"] == STEALTH_MODEL
    assert not (rabbit_env / "rabbit-backup.json").exists()


def test_dry_run_warns_that_rollback_would_fail_and_changes_nothing(rabbit_env: Path) -> None:
    rabbit, _, page = engine(stealth_page())
    result = run(rabbit.apply(model="openai/gpt-4o-mini", dry_run=True))
    assert result["ok"] and result["changed"] is False
    assert any(STEALTH_MODEL in w and "cannot be put back" in w for w in result["warnings"])
    assert page.paths == ["/api/byok/get"]


def test_a_listed_current_model_or_another_endpoint_is_not_flagged(rabbit_env: Path) -> None:
    for data in (
        fixture("byok_get_local_nexus_toplevel.json"),
        fixture("byok_get_local_other.json"),
    ):
        rabbit, _, _ = engine(FakeOs(data))
        result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5", dry_run=True))
        assert not any("cannot be put back" in w for w in result["warnings"])


def test_accept_no_rollback_goes_ahead_and_still_warns(rabbit_env: Path) -> None:
    rabbit, _, page = engine(stealth_page())
    result = run(rabbit.apply(model="openai/gpt-4o-mini", accept_no_rollback=True))
    assert result["ok"] and result["changed"]
    assert any("cannot be put back" in w for w in result["warnings"])
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"


def test_failed_register_with_an_unlisted_old_model_says_so_loudly(rabbit_env: Path) -> None:
    page = stealth_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini", accept_no_rollback=True))

    assert result["ok"] is False and result["code"] == "restore_failed"
    assert result["restore"]["outcome"] == "failed"
    assert "OS3 may have no provider" in result["hint"]
    assert page.state is None


def test_failed_register_rolls_back_a_listed_model(rabbit_env: Path) -> None:
    page = FakeOs(fixture("byok_get_local_nexus_toplevel.json"))
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))
    assert result["code"] == "restore_attempted"
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"


def applied_over_stealth(rabbit: RabbitSetup) -> None:
    assert run(rabbit.apply(model="openai/gpt-4o-mini", accept_no_rollback=True))["ok"]


def test_restore_of_an_unlisted_local_model_leaves_os3_untouched(rabbit_env: Path) -> None:
    rabbit, _, page = engine(stealth_page())
    applied_over_stealth(rabbit)
    deletes = page.paths.count("/api/byok/delete")

    result = run(rabbit.restore())

    assert result["ok"] is False and result["changed"] is False
    assert result["code"] == "restore_model_unavailable"
    assert STEALTH_MODEL in result["error"] and "Nothing was changed" in result["error"]
    assert page.paths.count("/api/byok/delete") == deletes
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"
    assert json.loads(backup_text(rabbit_env))["applied"] is not None


def test_restore_refuses_when_the_live_list_cannot_be_read(rabbit_env: Path) -> None:
    rabbit, _, page = engine(FakeOs(fixture("byok_get_local_nexus_toplevel.json")))
    run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    async def broken() -> list[dict[str, Any]]:
        raise OSError("down")

    down = RabbitSetup(browser=FakeBrowser(page), models_source=broken, retry_delay=0)
    result = run(down.restore())
    assert result["code"] == "restore_model_unavailable" and result["changed"] is False
    assert page.state["config"]["modelId"] == "anthropic/claude-sonnet-4.5"


def test_restore_refuses_a_local_backup_with_no_node_id(rabbit_env: Path) -> None:
    data = fixture("byok_get_local_nexus_toplevel.json")
    data["config"]["nodeId"] = None
    rabbit, _, page = engine(FakeOs(data))
    run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))
    (rabbit_env / "home" / ".rabbit-agent" / "connection.json").unlink()

    result = run(rabbit.restore())

    assert result["code"] == "restore_node_missing" and result["changed"] is False
    assert page.paths.count("/api/byok/delete") == 1
    assert page.state["config"]["modelId"] == "anthropic/claude-sonnet-4.5"


def restorable_openrouter(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RabbitSetup, FakeOs]:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))
    rabbit, _, page = engine(FakeOs(fixture("byok_get_custom_openrouter.json")))
    assert run(rabbit.apply(model="openai/gpt-4o-mini"))["ok"]
    return rabbit, page


def test_failed_restore_puts_the_current_connection_back(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rabbit, page = restorable_openrouter(rabbit_env, monkeypatch)
    page.fail_register_times = 1

    result = run(rabbit.restore())

    assert result["ok"] is False and result["code"] == "restore_failed"
    assert result["current_put_back"] is True and result["changed"] is False
    assert "was put back" in result["error"]
    assert page.state["provider"] == "local"
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"
    assert json.loads(backup_text(rabbit_env))["applied"] is not None
    assert BRIDGE_KEY not in json.dumps(result) and RESTORE_KEY not in json.dumps(result)


def test_failed_restore_reports_when_the_current_connection_is_lost_too(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rabbit, page = restorable_openrouter(rabbit_env, monkeypatch)
    page.fail_register_times = 2

    result = run(rabbit.restore())

    assert result["code"] == "restore_failed" and result["changed"] is True
    assert result["current_put_back"] is False
    assert "NOT be put back either" in result["error"]
    assert result["hint"].startswith("OS3 may have no provider")
    assert page.state is None


def test_failed_delete_during_restore_is_not_followed_by_a_put_back(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rabbit, page = restorable_openrouter(rabbit_env, monkeypatch)
    page.delete_status = 500
    registers = page.paths.count("/api/byok/register")

    result = run(rabbit.restore())

    assert result["code"] == "restore_failed" and result["current_put_back"] is None
    assert result["changed"] is False
    assert page.paths.count("/api/byok/register") == registers
    assert page.state["provider"] == "local"


def test_non_local_delete_is_first_sent_without_a_key(rabbit_env: Path) -> None:
    rabbit, _, page = engine(FakeOs(_custom_fixture()))
    assert run(rabbit.apply(model="openai/gpt-4o-mini"))["ok"]
    deletes = [b for p, b in page.calls if p == "/api/byok/delete"]
    assert deletes == [{"provider": "custom", "feature": "os3"}]


def test_non_local_delete_retries_once_with_the_provider_as_key(rabbit_env: Path) -> None:
    page = FakeOs(_custom_fixture())
    page.delete_needs_key = True
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is True and result["changed"] is True
    deletes = [b for p, b in page.calls if p == "/api/byok/delete"]
    assert deletes == [
        {"provider": "custom", "feature": "os3"},
        {"provider": "custom", "feature": "os3", "key": "custom"},
    ]
    assert page.state["provider"] == "local"


def test_non_local_delete_that_fails_twice_stops_with_nothing_changed(rabbit_env: Path) -> None:
    page = FakeOs(_custom_fixture())
    page.delete_status = 400
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is False and result["code"] == "delete_failed"
    assert result["changed"] is False
    assert page.paths.count("/api/byok/delete") == 2
    assert "/api/byok/register" not in page.paths
    assert page.state["provider"] == "custom"


def test_server_error_on_delete_is_not_retried(rabbit_env: Path) -> None:
    page = FakeOs(_custom_fixture())
    page.delete_status = 500
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))
    assert result["code"] == "delete_failed"
    assert page.paths.count("/api/byok/delete") == 1


def test_delete_that_reads_back_unchanged_is_unconfirmed_and_registers_nothing(
    rabbit_env: Path,
) -> None:
    page = FakeOs(_custom_fixture())
    page.delete_keeps_credential = True
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is False and result["code"] == "delete_unconfirmed"
    assert result["changed"] is False and result["restore"]["outcome"] == "unchanged"
    assert "/api/byok/register" not in page.paths
    assert page.state["provider"] == "custom"


def test_unconfirmed_delete_during_restore_registers_nothing(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rabbit, page = restorable_openrouter(rabbit_env, monkeypatch)
    page.delete_keeps_credential = True
    registers = page.paths.count("/api/byok/register")

    result = run(rabbit.restore())

    assert result["ok"] is False and result["changed"] is False
    assert result["cause"]["code"] == "delete_unconfirmed"
    assert page.paths.count("/api/byok/register") == registers
    assert page.state["provider"] == "local"


def use_restore_key(rabbit_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = rabbit_env / "restore.key"
    key_file.write_text(RESTORE_KEY + "\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(key_file))


UNREADABLE = [
    None,
    PageError("The OS3 tab is gone."),
    ManualRequired("signed_out", "No OS3 session token."),
]
UNREADABLE_IDS = ["http-503", "page-error", "signed-out"]


def never_says_unchanged(result: dict[str, Any]) -> None:
    text = " ".join(str(result.get(k, "")) for k in ("error", "hint", "restore")).lower()
    assert "unchanged" not in text and "never deleted" not in text


@pytest.mark.parametrize("error", UNREADABLE, ids=UNREADABLE_IDS)
@pytest.mark.parametrize("old", ["byok_get_local_nexus_toplevel.json", "custom"])
def test_unreadable_config_after_a_delete_puts_the_old_connection_back(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch, old: str, error: Exception | None
) -> None:
    use_restore_key(rabbit_env, monkeypatch)
    page = FakeOs(fixture("byok_get_custom_openrouter.json" if old == "custom" else old))
    provider = page.state["provider"]
    page.fail_gets_after_delete, page.get_error = 1, error
    rabbit, _, _ = engine(page)

    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    assert result["ok"] is False and result["code"] == "restore_attempted"
    assert result["changed"] is True and result["restore"]["outcome"] == "restored"
    assert result["cause"]["code"] == "os3_state_unknown"
    assert "put back" in result["error"]
    never_says_unchanged(result)
    assert page.state["provider"] == provider
    assert page.paths.count("/api/byok/register") == 1


@pytest.mark.parametrize("gets", [1, 3], ids=["register-refused", "read-back-unconfirmed"])
def test_unreadable_config_after_a_delete_and_no_put_back_is_state_unknown(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch, gets: int
) -> None:
    use_restore_key(rabbit_env, monkeypatch)
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    page.fail_gets_after_delete = gets
    page.fail_register_times = 1 if gets == 1 else 0
    rabbit, _, _ = engine(page)

    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    assert result["ok"] is False and result["mode"] == "auto"
    assert result["code"] == "os3_state_unknown" and result["changed"] is True
    assert result["error"].startswith("OS3 may have no provider connected")
    assert result["manual"]["base_url"] == ENDPOINT and result["url"]
    assert result["restore"]["outcome"] == "failed"
    never_says_unchanged(result)
    assert RESTORE_KEY not in json.dumps(result) and BRIDGE_KEY not in json.dumps(result)


@pytest.mark.parametrize("error", UNREADABLE, ids=UNREADABLE_IDS)
def test_unreadable_config_after_a_delete_during_restore_puts_the_current_back(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch, error: Exception | None
) -> None:
    rabbit, page = restorable_openrouter(rabbit_env, monkeypatch)
    page.fail_gets_after_delete, page.get_error = 1, error

    result = run(rabbit.restore())

    assert result["ok"] is False and result["code"] == "restore_failed"
    assert result["current_put_back"] is True and result["changed"] is False
    assert result["cause"]["code"] == "os3_state_unknown"
    assert "was put back" in result["error"]
    assert page.state["provider"] == "local"


@pytest.mark.parametrize("gets", [1, 3], ids=["register-refused", "read-back-unconfirmed"])
def test_unreadable_config_after_a_delete_during_restore_can_be_state_unknown(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch, gets: int
) -> None:
    rabbit, page = restorable_openrouter(rabbit_env, monkeypatch)
    page.fail_gets_after_delete = gets
    page.fail_register_times = 1 if gets == 1 else 0

    result = run(rabbit.restore())

    assert result["ok"] is False and result["mode"] == "auto"
    assert result["code"] == "os3_state_unknown" and result["changed"] is True
    assert result["current_put_back"] is False
    assert result["error"].startswith("OS3 may have no provider connected")
    assert result["manual"]["base_url"] == ENDPOINT and result["url"]
    never_says_unchanged(result)
    assert RESTORE_KEY not in json.dumps(result) and BRIDGE_KEY not in json.dumps(result)


class RefusesDeleteTwice(FakeOs):
    """Answers every delete 400, but really removes the credential when ``applies``."""

    def __init__(self, initial: Any, *, applies: bool) -> None:
        super().__init__(initial)
        self.applies = applies

    async def call(self, path: str, body: dict[str, Any]):
        kept = self.state
        reply = await super().call(path, body)
        if path == "/api/byok/delete":
            self.state = None if self.applies else kept
            return type(reply)(400, {"error": "refused"})
        return reply


def test_double_4xx_delete_reads_back_and_reports_the_old_connection_in_place(
    rabbit_env: Path,
) -> None:
    page = RefusesDeleteTwice(_custom_fixture(), applies=False)
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "delete_failed" and result["changed"] is False
    assert page.paths.count("/api/byok/delete") == 2
    assert page.paths[-1] == "/api/byok/get"
    assert "/api/byok/register" not in page.paths
    assert page.state["provider"] == "custom"


def test_double_4xx_delete_that_read_back_gone_carries_on_as_deleted(rabbit_env: Path) -> None:
    page = RefusesDeleteTwice(_custom_fixture(), applies=True)
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is True and result["changed"] is True
    assert page.state["provider"] == "local"
    assert page.paths.count("/api/byok/register") == 1


def test_double_4xx_delete_with_an_unreadable_config_and_the_connection_in_place(
    rabbit_env: Path,
) -> None:
    page = RefusesDeleteTwice(_custom_fixture(), applies=False)
    page.fail_gets_after_delete = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "delete_failed" and result["changed"] is False
    assert "/api/byok/register" not in page.paths
    assert page.state["provider"] == "custom"


def test_double_4xx_delete_with_an_unreadable_config_and_the_connection_gone(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_restore_key(rabbit_env, monkeypatch)
    page = RefusesDeleteTwice(_custom_fixture(), applies=True)
    page.fail_gets_after_delete = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "restore_attempted" and result["changed"] is True
    assert page.state["provider"] == "custom"
    never_says_unchanged(result)


def test_double_4xx_delete_with_an_unreadable_config_and_no_put_back_is_state_unknown(
    rabbit_env: Path,
) -> None:
    page = RefusesDeleteTwice(_custom_fixture(), applies=True)
    page.fail_gets_after_delete = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "os3_state_unknown" and result["changed"] is True
    assert page.state is None
    never_says_unchanged(result)


def test_every_non_local_backup_with_an_endpoint_is_registered_as_custom(
    rabbit_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_restore_key(rabbit_env, monkeypatch)
    data = _custom_fixture()
    data["credential"]["provider"] = "openrouter"
    page = FakeOs(data)
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["code"] == "restore_attempted" and result["restore"]["outcome"] == "restored"
    registers = [b for p, b in page.calls if p == "/api/byok/register"]
    assert registers[-1]["provider"] == "custom"
    assert registers[-1]["endpoint"] == "https://openrouter.ai/api/v1"
    assert page.state["provider"] == "custom"


def test_the_fake_os_rejects_the_old_request_shapes() -> None:
    page = FakeOs(_custom_fixture())
    old = run(page.call("/api/byok/update-model", {"provider": "custom", "tierModels": {}}))
    no_model = run(page.call("/api/byok/update-model", {"feature": "os3", "tier": "small"}))
    no_endpoint = run(page.register({"provider": "custom", "feature": "os3"}, RESTORE_KEY))
    odd = run(page.register({"provider": "openrouter", "endpoint": "https://x/v1"}, RESTORE_KEY))
    assert (old.status, no_model.status, no_endpoint.status, odd.status) == (400, 400, 400, 400)


def test_rollback_is_skipped_when_the_delete_never_ran(rabbit_env: Path) -> None:
    class DeleteRefusedThenGetBroken(FakeOs):
        async def call(self, path: str, body: dict[str, Any]):
            reply = await super().call(path, body)
            if path == "/api/byok/delete":
                self.get_status = 500
            return reply

    page = DeleteRefusedThenGetBroken(fixture("byok_get_local_nexus_toplevel.json"))
    page.delete_status = 500
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="anthropic/claude-sonnet-4.5"))

    assert result["ok"] is False and result["code"] == "delete_failed"
    assert result["restore"]["outcome"] == "unchanged"
    assert "/api/byok/register" not in page.paths
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"


@pytest.mark.parametrize(
    "error",
    [PageError("The OS3 tab is gone."), ManualRequired("signed_out", "No OS3 session token.")],
)
def test_read_back_failure_after_a_successful_register_is_not_manual(
    rabbit_env: Path, error: Exception
) -> None:
    page = FakeOs(fixture("byok_get_custom_openrouter.json"))
    original_register = page.register
    original_call = page.call
    registered = False

    async def register(body: dict[str, Any], key: str):
        nonlocal registered
        reply = await original_register(body, key)
        registered = registered or reply.ok
        return reply

    async def call(path: str, body: dict[str, Any]):
        if registered and path == "/api/byok/get":
            raise error
        return await original_call(path, body)

    page.register = register  # type: ignore[method-assign]
    page.call = call  # type: ignore[method-assign]
    rabbit, _, _ = engine(page)
    result = run(rabbit.apply(model="openai/gpt-4o-mini"))

    assert result["ok"] is True and result["mode"] == "auto" and result["changed"] is True
    assert any(w.startswith("verify_unconfirmed") for w in result["warnings"])
    assert "verified" not in result
    assert page.state["config"]["modelId"] == "openai/gpt-4o-mini"
    saved = json.loads(backup_text(rabbit_env))
    assert saved["applied"]["model"] == "openai/gpt-4o-mini"
    assert saved["before"]["provider"] == "custom"
