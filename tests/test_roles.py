"""Role detection and per-role model routing, against the OS3 replay fixtures."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.roles import ROLES, apply_role_model, classify, role_config

from tests.os3_cases import CASES
from tests.os3_replay import MODEL, Reply, load, replay
from tests.transport import HandlerTransport, key_ok

FIXTURE_ROLE = {"chat": "chat", "worker": "worker", "computer_use": "worker"}
PIXEL = "data:image/png;base64,iVBORw0KGgo="


def _image_user(text: str = "What colours are the four bands?") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": PIXEL}},
        ],
    }


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


@pytest.mark.parametrize("name", sorted(FIXTURE_ROLE))
def test_fixtures_classify_as_labelled(name: str) -> None:
    assert classify(load(name)) == FIXTURE_ROLE[name]


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_every_replay_case_routes_to_its_fixture_role(case) -> None:
    assert classify(load(case.fixture)) == FIXTURE_ROLE[case.fixture]


def test_the_computer_use_screenshot_is_worker_traffic_not_the_image_check() -> None:
    request = load("computer_use")
    assert any(
        isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
        for m in request["messages"]
    )
    assert classify(request) == "worker"


@pytest.mark.parametrize(
    ("payload", "role"),
    [
        ({"messages": [{"role": "user", "content": "Summarise this chat."}]}, "background"),
        ({"messages": [{"role": "user", "content": "x"}], "tools": []}, "background"),
        ({"messages": [_image_user()]}, "vision"),
        (
            {"messages": [{"role": "system", "content": "You check images."}, _image_user()]},
            "vision",
        ),
        ({"messages": [_image_user()], "tools": [_tool("emit_facts")]}, "background"),
        ({"messages": [_image_user()], "tools": [_tool("create_task")]}, "chat"),
        ({"messages": [], "tools": [_tool("emit_facts")]}, "background"),
        ({"messages": [], "tools": [_tool("report_correction")]}, "background"),
        ({"messages": [], "tools": [_tool("create_task"), _tool("shell")]}, "chat"),
        ({"messages": [], "tools": [_tool("shell")]}, "worker"),
        ({"messages": [], "tools": [_tool("notify_before_act")]}, "chat"),
        ({"messages": [], "tools": [_tool("something_new")]}, "chat"),
        ({"messages": [], "tools": [{"name": "shell"}]}, "worker"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_synthetic_shapes(payload: dict, role: str) -> None:
    assert classify(payload) == role


def test_worker_prompt_wins_without_tools_and_with_list_content() -> None:
    prompt = [{"type": "text", "text": "You are a Worker Agent in OS3."}]
    assert classify({"messages": [{"role": "system", "content": prompt}]}) == "worker"
    assert classify({"messages": [{"role": "developer", "content": prompt[0]["text"]}]}) == "worker"


def test_a_chat_prompt_that_mentions_workers_stays_chat() -> None:
    system = "You are the assistant. Hand device work to a worker agent via create_task."
    payload = {
        "messages": [{"role": "system", "content": system}],
        "tools": [_tool("create_task")],
    }
    assert classify(payload) == "chat"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": None, "tools": "shell"},
        {"messages": ["x", None, {"content": 3}], "tools": [None, 4, {"function": 5}]},
        {"messages": [{"role": "system", "content": [None, {"text": 4}]}]},
    ],
)
def test_malformed_bodies_classify_without_raising(payload: dict) -> None:
    assert classify(payload) in ROLES


def test_classification_does_not_mutate_the_request() -> None:
    request = load("worker")
    before = json.dumps(request, sort_keys=True)
    classify(request)
    assert json.dumps(request, sort_keys=True) == before


def test_nothing_configured_changes_nothing() -> None:
    config = role_config()
    assert not config.configured
    assert all(item.model is None and item.source == "os3" for item in config.roles.values())
    request = load("worker")
    apply_role_model(request)
    assert request == load("worker")


def test_env_sets_a_role(monkeypatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/worker-model:free")
    config = role_config()
    assert config.roles["worker"].model == "vendor/worker-model:free"
    assert config.roles["worker"].source == "env"
    assert config.roles["chat"].model is None


def test_file_sets_roles_and_env_wins_per_role(monkeypatch, tmp_path) -> None:
    (tmp_path / "roles.json").write_text(
        json.dumps({"chat": "file/chat", "background": "file/bg"}), encoding="utf-8"
    )
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "env/chat")
    config = role_config()
    assert (config.roles["chat"].model, config.roles["chat"].source) == ("env/chat", "env")
    assert (config.roles["background"].model, config.roles["background"].source) == (
        "file/bg",
        "file",
    )


def test_vision_follows_worker_unless_it_has_its_own(monkeypatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/worker")
    config = role_config()
    assert (config.roles["vision"].model, config.roles["vision"].source) == (
        "vendor/worker",
        "worker",
    )
    monkeypatch.setenv("OS3_BRIDGE_MODEL_VISION", "vendor/eyes")
    assert role_config().roles["vision"].model == "vendor/eyes"


def test_bad_settings_are_reported_and_never_applied(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "has space")
    (tmp_path / "roles.json").write_text(
        json.dumps({"workers": "x/y", "worker": 3}), encoding="utf-8"
    )
    config = role_config()
    assert config.configured
    assert config.roles["chat"].model is None
    assert config.roles["chat"].problem
    assert len(config.problems) == 3
    assert "has space" not in " ".join(config.problems)


def test_unreadable_file_is_a_problem(tmp_path) -> None:
    (tmp_path / "roles.json").write_text("{nope", encoding="utf-8")
    config = role_config()
    assert config.problems
    assert not any(item.model for item in config.roles.values())


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("name", sorted(FIXTURE_ROLE))
def test_role_override_wins_over_the_os3_model_field(
    name, stream, monkeypatch, bridge_key, openrouter_key, auth_header
) -> None:
    role = FIXTURE_ROLE[name]
    monkeypatch.setenv(f"OS3_BRIDGE_MODEL_{role.upper()}", "vendor/for-this-role:free")
    request = load(name)
    assert request["model"] == MODEL
    result = replay(request, [Reply(content="ok")], repair=True, stream=stream, headers=auth_header)
    assert result.status == 200
    assert result.upstream.requests[0] == {
        **request,
        "model": "vendor/for-this-role:free",
        "stream": stream,
    }


def test_other_roles_keep_the_os3_model(monkeypatch, bridge_key, openrouter_key, auth_header):
    monkeypatch.setenv("OS3_BRIDGE_MODEL_BACKGROUND", "vendor/bg")
    for name in ("chat", "worker"):
        result = replay(
            load(name), [Reply(content="ok")], repair=True, stream=False, headers=auth_header
        )
        assert result.upstream.requests[0]["model"] == MODEL


def test_background_and_image_check_are_routed(
    monkeypatch, bridge_key, openrouter_key, auth_header
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_BACKGROUND", "vendor/bg")
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/worker")
    base = {"model": MODEL, "messages": [{"role": "user", "content": "Extract facts."}]}
    seen = {}
    for label, request in {
        "background": base,
        "emit_facts": {**base, "tools": [_tool("emit_facts")]},
        "image": {**base, "messages": [_image_user()]},
    }.items():
        result = replay(
            request, [Reply(content="ok")], repair=True, stream=False, headers=auth_header
        )
        seen[label] = result.upstream.requests[0]["model"]
    assert seen == {
        "background": "vendor/bg",
        "emit_facts": "vendor/bg",
        "image": "vendor/worker",
    }
    monkeypatch.setenv("OS3_BRIDGE_MODEL_VISION", "vendor/eyes")
    result = replay(
        {**base, "messages": [_image_user()]},
        [Reply(content="ok")],
        repair=True,
        stream=False,
        headers=auth_header,
    )
    assert result.upstream.requests[0]["model"] == "vendor/eyes"


def test_a_request_with_no_model_gets_the_role_model(
    monkeypatch, bridge_key, openrouter_key, auth_header
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/worker")
    request = {k: v for k, v in load("worker").items() if k != "model"}
    result = replay(request, [Reply(content="ok")], repair=True, stream=False, headers=auth_header)
    assert result.upstream.requests[0]["model"] == "vendor/worker"


def test_tool_repair_still_runs_on_the_role_model(
    monkeypatch, bridge_key, openrouter_key, auth_header
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/worker")
    case = next(c for c in CASES if c.id == "worker-missing-command")
    result = replay(
        load(case.fixture), case.replies, repair=True, stream=False, headers=auth_header
    )
    assert [r["model"] for r in result.upstream.requests] == ["vendor/worker"] * 2


def test_the_log_names_the_role_and_nothing_else(
    monkeypatch, caplog, bridge_key, openrouter_key, auth_header
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/secret-looking-model")
    request = load("worker")
    with caplog.at_level(logging.DEBUG, logger="nexus"):
        replay(request, [Reply(content="ok")], repair=True, stream=False, headers=auth_header)
        replay(request, [Reply(content="ok")], repair=True, stream=False, headers=auth_header)
    lines = [r.getMessage() for r in caplog.records if "override" in r.getMessage()]
    assert lines == ["role=worker model override applied"] * 2
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert openrouter_key not in text
    assert "vendor/secret-looking-model" not in text
    assert "worker agent in OS3" not in text


def test_no_log_when_the_os3_model_already_matches(
    monkeypatch, caplog, bridge_key, openrouter_key, auth_header
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", MODEL)
    with caplog.at_level(logging.DEBUG, logger="nexus"):
        replay(
            load("worker"), [Reply(content="ok")], repair=True, stream=False, headers=auth_header
        )
    assert not [r for r in caplog.records if "override" in r.getMessage()]


def _catalog(*ids: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            data = [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in ids]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404, json={"error": {"message": "no"}})

    return HandlerTransport(key_ok(handler))


def _health(*ids: str) -> dict:
    from nexus.config import DEFAULT_MODEL

    app = create_app(backend=OpenRouterBackend(transport=_catalog(DEFAULT_MODEL, *ids)))
    with TestClient(app) as client:
        return client.get("/health").json()


def test_health_without_roles_is_unchanged(bridge_key, openrouter_key) -> None:
    body = _health()
    assert body["ready"] is True
    assert body["roles_ready"] is None
    assert body["roles_hint"] is None
    assert {r: e["model"] for r, e in body["roles"].items()} == dict.fromkeys(ROLES)
    assert all("ready" not in e for e in body["roles"].values())


def test_health_is_ready_when_every_role_model_is_live(
    monkeypatch, bridge_key, openrouter_key
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "vendor/chat")
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/worker")
    body = _health("vendor/chat", "vendor/worker")
    assert body["ready"] is True
    assert body["roles_ready"] is True
    assert body["roles"]["chat"] == {
        "model": "vendor/chat",
        "source": "env",
        "ready": True,
        "hint": "ok",
    }
    assert body["roles"]["vision"]["model"] == "vendor/worker"
    assert body["roles"]["vision"]["source"] == "worker"
    assert body["roles"]["background"]["model"] is None


def test_health_is_not_ready_when_a_role_model_is_missing(
    monkeypatch, bridge_key, openrouter_key
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "vendor/chat")
    monkeypatch.setenv("OS3_BRIDGE_MODEL_BACKGROUND", "vendor/gone")
    body = _health("vendor/chat")
    assert body["model_ready"] is True
    assert body["ready"] is False
    assert body["roles_ready"] is False
    assert body["roles"]["chat"]["ready"] is True
    assert body["roles"]["background"]["ready"] is False
    assert "vendor/gone" in body["roles_hint"]
    assert "NEXUS_MODEL_BACKGROUND" in body["roles_hint"]


def test_health_names_the_file_setting_for_a_file_role(
    monkeypatch, tmp_path, bridge_key, openrouter_key
) -> None:
    (tmp_path / "roles.json").write_text('{"worker": "vendor/gone"}', encoding="utf-8")
    body = _health()
    assert body["ready"] is False
    assert '"worker" in roles.json' in body["roles_hint"]
    assert body["roles"]["vision"]["ready"] is False


def test_health_is_not_ready_when_a_role_model_cannot_be_confirmed(
    monkeypatch, bridge_key, openrouter_key
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "vendor/chat")

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(down)))
    with TestClient(app) as client:
        response = client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert body["ready"] is False
    assert body["roles"]["chat"]["ready"] is None


def test_health_is_not_ready_on_a_bad_setting(monkeypatch, bridge_key, openrouter_key) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "bad id")
    body = _health()
    assert body["ready"] is False
    assert body["roles"]["chat"]["ready"] is False
    assert "NEXUS_MODEL_CHAT" in body["roles_hint"]


def test_doctor_carries_the_roles(monkeypatch, bridge_key, openrouter_key) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_CHAT", "vendor/chat")
    from nexus.config import DEFAULT_MODEL

    app = create_app(backend=OpenRouterBackend(transport=_catalog(DEFAULT_MODEL, "vendor/chat")))
    with TestClient(app) as client:
        body = client.get("/doctor").json()
    assert body["roles"]["chat"]["ready"] is True


def test_dashboard_reads_the_roles_from_health() -> None:
    from nexus.dashboard import DASHBOARD_HTML

    assert 'id="role-models"' in DASHBOARD_HTML
    assert "roleProblem(h)" in DASHBOARD_HTML
