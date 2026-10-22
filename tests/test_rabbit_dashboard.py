"""GET /dashboard/rabbit and POST /dashboard/rabbit/{apply,restore} over a fake OS page."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.rabbit_cdp import ManualRequired
from nexus.rabbit_setup import RabbitSetup

from tests.rabbit_fakes import (
    BRIDGE_KEY,
    STEALTH_MODEL,
    FakeBrowser,
    FakeOs,
    fixture,
    models_source,
)
from tests.test_rabbit_setup import rabbit_env  # noqa: F401

JSON = {"Content-Type": "application/json"}


class Harness:
    def __init__(self, page: FakeOs, unavailable: ManualRequired | None = None) -> None:
        self.page = page
        self.built = 0
        self.unavailable = unavailable
        self.app = create_app()
        self.app.state.rabbit_factory = self.factory
        self.client = TestClient(self.app)

    def factory(self) -> RabbitSetup:
        self.built += 1
        return RabbitSetup(
            browser=FakeBrowser(self.page, unavailable=self.unavailable),
            models_source=models_source,
            retry_delay=0,
        )


@pytest.fixture
def door() -> Harness:
    return Harness(FakeOs(fixture("byok_get_custom_openrouter.json")))


def post(door: Harness, path: str, body: Any = None, **kwargs: Any):
    payload = json.dumps(body if body is not None else {})
    return door.client.post(path, content=payload, headers={**JSON, **kwargs.pop("headers", {})})


def test_read_returns_plan_status_opt_in_and_model_billing(door: Harness) -> None:
    reply = door.client.get("/dashboard/rabbit")
    body = reply.json()

    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert body["ok"] is True and body["opt_in"] is True
    assert body["plan"]["base_url"] == "http://127.0.0.1:11435/v1"
    assert body["plan"]["model"] == "meituan/longcat-2.5-preview:free"
    billing = {row["id"]: row["billing"] for row in body["plan"]["models"]}
    assert billing["meituan/longcat-2.5-preview:free"] == "free"
    assert billing["openai/gpt-4o-mini"] == "unknown"
    assert body["status"]["opt_in"] is True and body["status"]["cdp"]["port"] == 9333
    assert BRIDGE_KEY not in reply.text
    assert door.page.calls == []


def test_read_accepts_a_model_to_plan_and_warns_when_billed(door: Harness) -> None:
    body = door.client.get("/dashboard/rabbit", params={"model": "openai/gpt-4o-mini"}).json()
    assert body["plan"]["model"] == "openai/gpt-4o-mini"
    assert any("may be billed" in w for w in body["plan"]["warnings"])


def test_read_works_with_the_opt_in_off(door: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OS3_BRIDGE_RABBIT_SETUP")
    body = door.client.get("/dashboard/rabbit").json()
    assert body["ok"] is True and body["opt_in"] is False and body["plan"]["ok"] is True


def test_apply_dry_run_has_the_engine_shape_and_masks_the_key(door: Harness) -> None:
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini", "dry_run": True})
    body = reply.json()

    assert reply.status_code == 200 and body["ok"] is True and body["dry_run"] is True
    assert body["diff"]["after"] == {
        "provider": "local",
        "endpoint": "http://127.0.0.1:11435/v1",
        "modelId": "openai/gpt-4o-mini",
        "contextWindow": 65536,
        "key": "****c1d7",
    }
    assert body["diff"]["before"]["provider"] == "custom"
    assert BRIDGE_KEY not in reply.text
    assert door.page.paths == ["/api/byok/get"]


def test_apply_registers_and_the_key_never_comes_back(door: Harness) -> None:
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"})
    assert reply.status_code == 200 and reply.json()["changed"] is True
    assert door.page.state["provider"] == "local"
    assert door.page.registered_keys == [BRIDGE_KEY]
    assert BRIDGE_KEY not in reply.text


def test_restore_puts_back_the_saved_connection(door: Harness) -> None:
    door.page.state = fixture("byok_get_local_nexus_toplevel.json")
    assert post(door, "/dashboard/rabbit/apply", {"model": "anthropic/claude-sonnet-4.5"}).json()[
        "ok"
    ]
    reply = post(door, "/dashboard/rabbit/restore")
    assert reply.status_code == 200 and reply.json()["ok"] is True
    assert door.page.state["config"]["modelId"] == "openai/gpt-4o-mini"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"}),
        ("/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini", "dry_run": True}),
        ("/dashboard/rabbit/restore", {}),
    ],
)
def test_writes_are_refused_with_a_hint_while_the_opt_in_is_off(
    door: Harness, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    monkeypatch.delenv("OS3_BRIDGE_RABBIT_SETUP")
    reply = post(door, path, body)
    assert reply.status_code == 403
    assert reply.json()["ok"] is False and reply.json()["code"] == "opt_in_required"
    assert "NEXUS_RABBIT_SETUP=on" in reply.json()["hint"]
    assert door.page.calls == []


def test_unknown_model_is_a_400_listing_the_closest_ids(door: Harness) -> None:
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mni"})
    body = reply.json()
    assert reply.status_code == 400 and body["code"] == "unknown_model"
    assert body["closest"][0] == "openai/gpt-4o-mini"
    assert door.page.calls == []


def test_manual_fallback_is_a_200_the_ui_can_show(door: Harness) -> None:
    door.page.signed_out = True
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"})
    body = reply.json()
    assert reply.status_code == 200
    assert body["ok"] is False and body["mode"] == "manual" and body["code"] == "signed_out"
    assert body["url"] == "https://os3.rabbit.tech/settings/api-keys"
    assert body["manual"]["model_id"] == "openai/gpt-4o-mini"
    assert BRIDGE_KEY not in reply.text


def test_failure_after_delete_is_a_502_with_the_envelope(door: Harness) -> None:
    door.page.fail_register_times = 1
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"})
    body = reply.json()
    assert reply.status_code == 502 and body["ok"] is False
    assert body["code"] == "restore_failed" and body["hint"] and body["error"]
    assert BRIDGE_KEY not in reply.text


def test_state_unknown_is_a_502_with_the_manual_values(
    door: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    restore_key = tmp_path / "restore.key"
    restore_key.write_text("sk-or-v1-FIXTURE-restore-key-wxyz\n")
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(restore_key))
    door.page.fail_gets_after_delete, door.page.fail_register_times = 1, 1
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"})
    body = reply.json()
    assert reply.status_code == 502 and body["ok"] is False
    assert body["code"] == "os3_state_unknown" and body["manual"]["base_url"] and body["url"]
    assert BRIDGE_KEY not in reply.text


def test_a_second_change_while_one_runs_is_a_409(door: Harness) -> None:
    door.app.state.rabbit_running = True
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"})
    assert reply.status_code == 409 and reply.json()["code"] == "rabbit_busy"
    door.app.state.rabbit_running = False
    assert post(door, "/dashboard/rabbit/apply", {"dry_run": True}).status_code == 200
    assert door.app.state.rabbit_running is False


def test_the_lock_is_released_after_a_refusal(door: Harness) -> None:
    assert post(door, "/dashboard/rabbit/apply", {"model": "nope"}).status_code == 400
    assert door.app.state.rabbit_running is False


@pytest.mark.parametrize(
    "body",
    [{"model": 5}, {"dry_run": "yes"}, {"model": ["a"]}, [], "text"],
)
def test_bad_bodies_are_400(door: Harness, body: Any) -> None:
    reply = post(door, "/dashboard/rabbit/apply", body)
    assert reply.status_code == 400
    assert reply.json()["code"] in {"invalid_request", "invalid_json"}
    assert door.page.calls == []


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, None, [], {}])
def test_accept_no_rollback_must_be_a_strict_boolean(door: Harness, value: Any) -> None:
    reply = post(door, "/dashboard/rabbit/apply", {"accept_no_rollback": value})
    assert reply.status_code == 400
    assert reply.json()["code"] == "invalid_request" and reply.json()["hint"]
    assert door.page.calls == []


def stealth_door() -> Harness:
    return Harness(FakeOs(fixture("byok_get_local_stealth.json")))


def test_apply_refuses_with_409_when_the_current_model_cannot_be_rolled_back() -> None:
    door = stealth_door()
    reply = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini"})
    body = reply.json()

    assert reply.status_code == 409 and reply.headers["cache-control"] == "no-store"
    assert body["ok"] is False and body["code"] == "rollback_unavailable"
    assert STEALTH_MODEL in body["error"] and body["hint"]
    assert body["manual"]["base_url"] == "http://127.0.0.1:11435/v1"
    assert body["manual"]["model_id"] == "openai/gpt-4o-mini"
    assert body["url"] == "https://os3.rabbit.tech/settings/api-keys"
    assert BRIDGE_KEY not in reply.text
    assert door.page.paths == ["/api/byok/get"]
    assert door.page.state["config"]["modelId"] == STEALTH_MODEL
    assert door.app.state.rabbit_running is False


def test_accept_no_rollback_true_lets_apply_through() -> None:
    door = stealth_door()
    reply = post(
        door,
        "/dashboard/rabbit/apply",
        {"model": "openai/gpt-4o-mini", "accept_no_rollback": True},
    )
    body = reply.json()

    assert reply.status_code == 200 and body["ok"] is True and body["changed"] is True
    assert any("cannot be put back" in w for w in body["warnings"])
    assert door.page.state["config"]["modelId"] == "openai/gpt-4o-mini"


def test_accept_no_rollback_false_is_the_default_and_the_dry_run_only_warns() -> None:
    door = stealth_door()
    explicit = post(
        door,
        "/dashboard/rabbit/apply",
        {"model": "openai/gpt-4o-mini", "accept_no_rollback": False},
    )
    assert explicit.status_code == 409 and explicit.json()["code"] == "rollback_unavailable"

    dry = post(door, "/dashboard/rabbit/apply", {"model": "openai/gpt-4o-mini", "dry_run": True})
    assert dry.status_code == 200 and dry.json()["changed"] is False
    assert any("cannot be put back" in w for w in dry.json()["warnings"])


def test_non_json_body_is_a_400(door: Harness) -> None:
    reply = door.client.post("/dashboard/rabbit/apply", content=b"{nope", headers=JSON)
    assert reply.status_code == 400 and reply.json()["code"] == "invalid_json"


@pytest.mark.parametrize("path", ["/dashboard/rabbit/apply", "/dashboard/rabbit/restore"])
@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"Origin": "http://evil.example"}, "origin_mismatch"),
        ({"Origin": "null"}, "origin_mismatch"),
        ({"Sec-Fetch-Site": "cross-site"}, "cross_site_refused"),
        ({"Host": "evil.example:11435"}, "host_refused"),
        ({"Content-Type": "text/plain"}, "json_required"),
    ],
)
def test_cross_origin_and_foreign_host_writes_never_reach_the_engine(
    door: Harness, path: str, headers: dict[str, str], code: str
) -> None:
    reply = door.client.post(path, content="{}", headers={**JSON, **headers})
    assert reply.status_code == 403 and reply.json()["code"] == code
    assert door.built == 0 and door.page.calls == []


def test_foreign_host_cannot_read_the_plan(door: Harness) -> None:
    reply = door.client.get("/dashboard/rabbit", headers={"Host": "evil.example"})
    assert reply.status_code == 403 and reply.json()["code"] == "host_refused"
    assert door.built == 0


def test_same_origin_write_with_a_matching_origin_header_is_allowed(door: Harness) -> None:
    reply = post(
        door,
        "/dashboard/rabbit/apply",
        {"dry_run": True},
        headers={"Origin": "http://127.0.0.1:11435", "Sec-Fetch-Site": "same-origin"},
    )
    assert reply.status_code == 200


def test_the_routes_need_no_bearer_and_v1_is_untouched(door: Harness) -> None:
    assert door.client.get("/dashboard/rabbit").status_code == 200
    assert door.client.get("/v1/models").status_code == 401
