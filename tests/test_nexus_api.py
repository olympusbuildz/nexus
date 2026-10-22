"""docs/ui-api.md: the control-plane fields and endpoints the Nexus UI builds against."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import pulse as pulse_module
from nexus.app import create_app
from nexus.backend_state import STATES
from nexus.backends.openrouter import OpenRouterBackend
from nexus.errors import GatewayError
from nexus.pulse import PulseLog, note_error
from nexus.setup_flow import VerifyResult

from tests.test_subscription import _install_cli
from tests.transport import HandlerTransport

CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
LONG_KEY = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789c2f9"


class Fake:
    """A backend the tests drive. ``id`` is the backend it pretends to be."""

    def __init__(self, backend_id: str = "openrouter", *, ready: bool = True) -> None:
        self.id = backend_id
        self.ready = ready
        self.calls: list[dict[str, Any]] = []
        self.fail: GatewayError | None = None
        self.delay = 0.0
        self.usage: dict[str, Any] = {"prompt_tokens": 12, "completion_tokens": 40}
        self.text = "ok"
        self.kind = "rest"

    def describe(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "ready": self.ready,
            "hint": "ok" if self.ready else "fix it",
        }
        if self.kind == "cli":
            row["kind"] = "cli"
        return row

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    async def open_chat(self, payload: dict[str, Any], *, stream: bool):
        from nexus.backends.base import ChatUpstream

        self.calls.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        import json

        body = {
            "choices": [{"message": {"role": "assistant", "content": self.text}}],
            "usage": self.usage,
        }
        return ChatUpstream(200, "application/json", body=json.dumps(body).encode())


def _auth(bridge_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bridge_key}"}


def test_health_rows_carry_name_kind_and_state() -> None:
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
    assert health["ui_api"] == 1
    rows = health["backends"]
    assert rows["openrouter"]["name"] == "OpenRouter"
    assert rows["openrouter"]["kind"] == "rest"
    assert rows["openrouter"]["state"] == "not_configured"
    assert rows["codex"]["name"] == "Codex"
    assert rows["codex"]["kind"] == "cli"
    assert rows["codex"]["state"] == "cli_missing"
    names = {row["name"] for row in rows.values()}
    assert names == {
        "OpenRouter",
        "ChatGPT plan",
        "OpenAI",
        "Anthropic",
        "xAI",
        "Nous Portal",
        "Codex",
        "Claude Code",
        "GitHub Copilot",
        "Grok Build",
        "Antigravity",
    }
    assert all(row["state"] in STATES for row in rows.values())
    assert health["openrouter"]["state"] == "not_configured"


def test_existing_row_fields_are_untouched(openrouter_key: str) -> None:
    with TestClient(create_app()) as client:
        row = client.get("/health").json()["backends"]["openrouter"]
    assert row["key_present"] is True
    assert row["key_source"] == "env"
    assert row["ready"] is True
    assert row["hint"] == "ok"
    assert row["state"] == "ready"
    assert openrouter_key not in str(row)


def test_doctor_rows_carry_state_too(openrouter_key: str) -> None:
    with TestClient(create_app()) as client:
        doctor = client.get("/doctor").json()
    assert doctor["ui_api"] == 1
    assert doctor["backends"]["openrouter"]["state"] == "ready"


def test_rejected_key_on_the_active_backend_is_not_configured(openrouter_key: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(401, json={"error": {"message": "User not found."}})
        return httpx.Response(200, json={"data": []})

    backend = OpenRouterBackend(transport=HandlerTransport(handler))
    with TestClient(create_app(backend=backend)) as client:
        health = client.get("/health").json()
    row = health["backends"]["openrouter"]
    assert health["upstream_status"] == 401
    assert row["state"] == "not_configured"
    assert "rejected the API key" in row["hint"]
    assert row["ready"] is True
    assert openrouter_key not in str(health)


def test_cli_states_follow_the_login_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_cli(tmp_path, monkeypatch, mode="logged_out")
    with TestClient(create_app()) as client:
        row = client.get("/health").json()["backends"]["codex"]
    assert row["state"] == "not_logged_in"
    assert row["cli_present"] is True
    assert "codex login" in row["hint"]

    _install_cli(tmp_path, monkeypatch, mode="ok")
    with TestClient(create_app()) as client:
        row = client.get("/health").json()["backends"]["codex"]
    assert row["state"] == "ready"
    assert row["hint"] == "ok"


def _limit_error() -> GatewayError:
    return GatewayError(429, "usage limit reached", "rate_limit_error", "openrouter_usage_limit")


def test_out_of_quota_comes_from_traffic_and_clears_on_success(
    bridge_key: str, openrouter_key: str
) -> None:
    fake = Fake()
    with TestClient(create_app(backend=fake)) as client:
        fake.fail = _limit_error()
        limited = client.post("/v1/chat/completions", headers=_auth(bridge_key), json=CHAT)
        row = client.get("/health").json()["backends"]["openrouter"]
        assert limited.status_code == 429
        assert row["state"] == "out_of_quota"
        assert row["hint"] == (
            "Usage limit reached on OpenRouter. Wait for the window to reset, or switch backend."
        )
        assert row["ready"] is True

        fake.fail = None
        client.post("/v1/chat/completions", headers=_auth(bridge_key), json=CHAT)
        assert client.get("/health").json()["backends"]["openrouter"]["state"] == "ready"


def test_a_read_does_not_clear_out_of_quota(bridge_key: str, openrouter_key: str) -> None:
    fake = Fake()
    fake.fail = _limit_error()
    with TestClient(create_app(backend=fake)) as client:
        client.post("/v1/chat/completions", headers=_auth(bridge_key), json=CHAT)
        client.get("/v1/models", headers=_auth(bridge_key))
        state = client.get("/health").json()["backends"]["openrouter"]["state"]
    assert state == "out_of_quota"


def test_out_of_quota_expires_after_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(pulse_module.time, "monotonic", lambda: clock[0])
    log = PulseLog()
    log.record(
        method="POST",
        path="/v1/chat/completions",
        model="m",
        status=429,
        latency_ms=5,
        backend="codex",
    )
    assert log.quota_hit("codex") is True
    assert log.quota_hit("openrouter") is False
    clock[0] += pulse_module.QUOTA_COOLDOWN_S + 1
    assert log.quota_hit("codex") is False


def test_pulse_entries_name_the_backend_and_the_failure(
    bridge_key: str, openrouter_key: str
) -> None:
    fake = Fake()
    with TestClient(create_app(backend=fake)) as client:
        client.post("/v1/chat/completions", headers=_auth(bridge_key), json=CHAT)
        fake.fail = _limit_error()
        client.post("/v1/chat/completions", headers=_auth(bridge_key), json=CHAT)
        client.get("/v1/models")
        entries = client.get("/dashboard/pulse").json()["entries"]
    unauth, limited, ok = entries
    assert ok["backend"] == "openrouter"
    assert ok["error_code"] is None
    assert ok["hint"] is None
    assert ok["status"] == 200
    assert limited["backend"] == "openrouter"
    assert limited["error_code"] == "openrouter_usage_limit"
    assert limited["hint"] == (
        "The upstream is rate limiting or out of quota. Wait and retry, or GET /doctor."
    )
    assert limited["usage_limit"] is True
    assert unauth["status"] == 401
    assert unauth["backend"] is None
    assert unauth["error_code"] == "missing_api_key"
    assert "Bearer" in unauth["hint"]


def test_pulse_failure_before_dispatch_has_no_backend(bridge_key: str) -> None:
    with TestClient(create_app(backend=Fake())) as client:
        client.post(
            "/v1/chat/completions",
            headers={**_auth(bridge_key), "content-type": "application/json"},
            content=b"{nope",
        )
        entry = client.get("/dashboard/pulse").json()["entries"][0]
    assert entry["backend"] is None
    assert entry["error_code"] == "invalid_json"


def test_pulse_never_stores_a_secret_in_the_hint_or_a_bad_code(bridge_key: str) -> None:
    log = PulseLog()
    note_error("Not A Code!", f"use Bearer {bridge_key}-and-more-text to retry")
    log.record(method="POST", path="/v1/chat/completions", model="m", status=500, latency_ms=1)
    assert log.snapshot()[0]["error_code"] is None
    log.record(
        method="POST",
        path="/v1/chat/completions",
        model="m",
        status=500,
        latency_ms=1,
        error_code="x_failed",
        hint="a" * 900,
    )
    assert log.snapshot()[0]["error_code"] == "x_failed"


def test_note_error_redacts_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextvars import copy_context

    def run() -> tuple[Any, Any]:
        note_error("good_code", "Bearer abcdefghijklmnop12345 " + "x" * 900)
        return pulse_module._error_code.get(), pulse_module._error_hint.get()

    code, hint = copy_context().run(run)
    assert code == "good_code"
    assert "abcdefghijklmnop12345" not in hint
    assert len(hint) <= 400


def test_models_rows_name_their_backend() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/dashboard/models?backend=openrouter").json()
        cli = client.get("/dashboard/models?backend=codex").json()
    assert body["backend"] == "openrouter"
    assert body["models"]
    assert all(row["backend"] == "openrouter" for row in body["models"])
    assert all(set(row) == {"id", "status", "backend"} for row in body["models"])
    assert all(row["backend"] == "codex" for row in cli["models"])


def test_live_models_rows_name_their_backend(openrouter_key: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "vendor/live-one", "object": "model", "created": 1}]},
        )

    backend = OpenRouterBackend(transport=HandlerTransport(handler))
    import nexus.dashboard_models as dm

    original = dm.build_backend
    dm.build_backend = lambda name=None, **_kw: backend
    try:
        with TestClient(create_app()) as client:
            body = client.get("/dashboard/models?backend=openrouter").json()
    finally:
        dm.build_backend = original
    assert body["source"] == "live"
    assert {"id": "vendor/live-one", "status": "live", "backend": "openrouter"} in body["models"]


def test_config_read_reports_sources_without_a_key(tmp_path: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/config")
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["ok"] is True
    assert body["backend"] == "openrouter"
    assert body["backend_source"] == "env"
    assert body["model_source"] == "default"
    assert body["choice_file"] == str(tmp_path / "backend-choice")
    assert body["model_file"] == str(tmp_path / "model-choice")
    assert set(body["keys"]) == {"openrouter", "openai", "anthropic", "xai", "nous"}
    assert body["keys"]["openrouter"] == {
        "present": False,
        "source": None,
        "file": str(tmp_path / "missing-openrouter.key"),
        "mode": None,
        "masked": None,
    }
    assert body["restart_available"] is False
    assert body["restart_managed"] is False
    assert isinstance(body["restart_note"], str)


def test_config_read_masks_keys_to_the_last_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_file = tmp_path / "or.key"
    long_file.write_text(LONG_KEY + "\n", encoding="utf-8")
    long_file.chmod(0o600)
    short_file = tmp_path / "oa.key"
    short_file.write_text("sk-short-1234\n", encoding="utf-8")
    short_file.chmod(0o644)
    monkeypatch.setenv("OS3_BRIDGE_OPENROUTER_KEY_FILE", str(long_file))
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(short_file))
    env_key = "xai-EXAMPLE-not-a-real-key-zzzz"
    monkeypatch.setenv("XAI_API_KEY", env_key)
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/config")
    keys = response.json()["keys"]
    assert keys["openrouter"]["masked"] == "\u2022\u2022\u2022\u2022c2f9"
    assert keys["openrouter"]["source"] == "file"
    assert keys["openrouter"]["mode"] == "600"
    assert keys["openai"]["masked"] == "\u2022\u2022\u2022\u2022"
    assert keys["openai"]["mode"] == "644"
    assert keys["xai"]["source"] == "env"
    assert keys["xai"]["masked"] == "\u2022\u2022\u2022\u2022zzzz"
    for secret in (LONG_KEY, "sk-short-1234", env_key):
        assert secret not in response.text
    assert LONG_KEY[:-4] not in response.text


def test_config_read_says_where_backend_and_model_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    (tmp_path / "backend-choice").write_text("openai\n", encoding="utf-8")
    (tmp_path / "model-choice").write_text("gpt-4o\n", encoding="utf-8")
    with TestClient(create_app()) as client:
        body = client.get("/dashboard/config").json()
        monkeypatch.setenv("OS3_BRIDGE_MODEL", "pinned-model")
        pinned = client.get("/dashboard/config").json()
    assert (body["backend"], body["backend_source"]) == ("openai", "file")
    assert (body["model"], body["model_source"]) == ("gpt-4o", "file")
    assert (pinned["model"], pinned["model_source"]) == ("pinned-model", "env")


def test_config_read_is_guarded() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/config", headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert response.json()["code"] == "host_refused"


def test_activate_false_saves_a_key_and_leaves_the_choice_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_file))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    (tmp_path / "backend-choice").write_text("openrouter\n", encoding="utf-8")
    (tmp_path / "model-choice").write_text("keep/me\n", encoding="utf-8")
    typed = "sk-proj-activate-false-0123456789"
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": typed, "model": "ignored", "activate": False},
        )
        config = client.get("/dashboard/config").json()
    body = saved.json()
    assert saved.status_code == 200
    assert body["ok"] is True
    assert body["activated"] == "no"
    assert body["key_written"] == "yes"
    assert body["key_file"] == str(key_file)
    assert body["backend"] == "openrouter"
    assert body["model"] == "keep/me"
    assert typed not in saved.text
    assert (tmp_path / "backend-choice").read_text(encoding="utf-8").strip() == "openrouter"
    assert (tmp_path / "model-choice").read_text(encoding="utf-8").strip() == "keep/me"
    assert key_file.read_text(encoding="utf-8").strip() == typed
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert config["keys"]["openai"]["present"] is True
    assert typed not in str(config)


def test_activate_defaults_to_true_and_reports_it(tmp_path: Path) -> None:
    (tmp_path / "missing-openrouter.key").write_text("sk-or-v1-existing-key-0123456789\n")
    with TestClient(create_app()) as client:
        body = client.post("/dashboard/config", json={"backend": "openrouter"}).json()
    assert body["ok"] is True
    assert body["activated"] == "yes"
    assert body["backend"] == "openrouter"


def test_activate_false_refuses_a_cli_and_a_missing_key() -> None:
    with TestClient(create_app()) as client:
        cli = client.post("/dashboard/config", json={"backend": "codex", "activate": False})
        bare = client.post("/dashboard/config", json={"backend": "openai", "activate": False})
        wrong = client.post("/dashboard/config", json={"backend": "openai", "activate": "no"})
    assert (cli.status_code, cli.json()["code"]) == (400, "cli_has_no_key")
    assert (bare.status_code, bare.json()["code"]) == (400, "key_missing")
    assert (wrong.status_code, wrong.json()["code"]) == (400, "invalid_request")


def test_activate_false_still_verifies_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus import setup_flow

    monkeypatch.setattr(
        setup_flow,
        "verify_provider_key",
        lambda b, k: VerifyResult(False, "OpenAI rejected the key (HTTP 401)"),
    )
    key_file = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_file))
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-refused-0123456789", "activate": False},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "key_rejected"
    assert not key_file.exists()


def test_omitting_model_still_clears_the_saved_model(tmp_path: Path) -> None:
    """Documented behaviour in docs/ui-api.md 4.2: omitted or blank means reset."""
    (tmp_path / "missing-openrouter.key").write_text("sk-or-v1-existing-key-0123456789\n")
    (tmp_path / "model-choice").write_text("vendor/model\n", encoding="utf-8")
    with TestClient(create_app()) as client:
        body = client.post("/dashboard/config", json={"backend": "openrouter"}).json()
    assert body["model"] == ""
    assert not (tmp_path / "model-choice").exists()


def _assert_envelope(body: dict[str, Any], code: str) -> None:
    assert body["ok"] is False
    assert isinstance(body["error"], str) and body["error"]
    assert body["code"] == code
    assert isinstance(body["hint"], str) and body["hint"]


def test_guard_refusals_carry_code_and_hint() -> None:
    with TestClient(create_app()) as client:
        host = client.get("/health", headers={"Host": "evil.example"})
        site = client.post("/dashboard/config", json={}, headers={"Sec-Fetch-Site": "cross-site"})
        origin = client.post(
            "/dashboard/config", json={}, headers={"Origin": "https://evil.example"}
        )
        ctype = client.post(
            "/dashboard/config", content="{}", headers={"Content-Type": "text/plain"}
        )
    expected = {
        host: ("host_refused", "Host must be 127.0.0.1 or localhost."),
        site: ("cross_site_refused", "Cross-site writes are refused."),
        origin: ("origin_mismatch", "Origin does not match this door."),
        ctype: ("json_required", "Writes must be sent as application/json."),
    }
    for response, (code, message) in expected.items():
        assert response.status_code == 403
        _assert_envelope(response.json(), code)
        assert response.json()["error"] == message


def test_config_errors_carry_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import setup_flow

    cases: list[tuple[dict[str, Any], str]] = [
        ({"backend": ""}, "invalid_request"),
        ({"backend": "nope"}, "unknown_backend"),
        ({"backend": "openai"}, "key_missing"),
        ({"backend": "openai", "api_key": "has space inside"}, "key_invalid"),
        ({"backend": "openai", "api_key": "changeme"}, "key_invalid"),
        (
            {"backend": "openai", "api_key": "sk-test-FAKE-0123456789abcdef", "model": "bad model"},
            "invalid_request",
        ),
        ({"backend": "openai", "api_key": 5}, "invalid_request"),
        ({"backend": "codex", "api_key": "sk-test-FAKE-0123456789abcdef"}, "cli_has_no_key"),
        ({"backend": "codex"}, "cli_missing"),
    ]
    with TestClient(create_app()) as client:
        for payload, code in cases:
            response = client.post("/dashboard/config", json=payload)
            assert response.status_code == 400, payload
            _assert_envelope(response.json(), code)
        notjson = client.post(
            "/dashboard/config", content=b"[1]", headers={"content-type": "application/json"}
        )
        broken = client.post(
            "/dashboard/config", content=b"{", headers={"content-type": "application/json"}
        )
        monkeypatch.setattr(
            setup_flow,
            "verify_provider_key",
            lambda b, k: VerifyResult(False, "refused (HTTP 401)"),
        )
        rejected = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-test-FAKE-0123456789abcdef"},
        )
        monkeypatch.setattr(
            setup_flow, "verify_provider_key", lambda b, k: VerifyResult(None, "could not reach it")
        )
        unverified = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-test-FAKE-0123456789abcdef"},
        )
    _assert_envelope(notjson.json(), "invalid_json")
    _assert_envelope(broken.json(), "invalid_json")
    _assert_envelope(rejected.json(), "key_rejected")
    _assert_envelope(unverified.json(), "key_unverified")
    assert not (tmp_path / "backend-choice").exists()


def test_unknown_backend_on_models_keeps_404_and_gains_code() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/models?backend=nope")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    _assert_envelope(response.json(), "unknown_backend")
    assert response.json()["error"] == "Unknown backend 'nope'."


def test_unexpected_failure_outside_v1_uses_the_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.app as app_module

    def boom() -> dict:
        raise RuntimeError("secret-detail-sk-should-not-leak")

    monkeypatch.setattr(app_module, "config_payload", boom)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.get("/dashboard/config")
    assert response.status_code == 500
    _assert_envelope(response.json(), "internal_error")
    assert "secret-detail" not in response.text


def test_unexpected_failure_inside_v1_keeps_the_openai_shape(bridge_key: str) -> None:
    class Broken(Fake):
        async def list_models(self) -> list:
            raise RuntimeError("boom")

    with TestClient(create_app(backend=Broken()), raise_server_exceptions=False) as client:
        response = client.get("/v1/models", headers=_auth(bridge_key))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["type"] == "internal_error"
    assert "ok" not in response.json()


def test_framework_404_and_405_are_left_alone() -> None:
    with TestClient(create_app()) as client:
        missing = client.get("/not-a-route")
        wrong = client.put("/dashboard/config", json={})
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
    assert wrong.status_code == 405
    assert wrong.json() == {"detail": "Method Not Allowed"}


def _patch_backends(monkeypatch: pytest.MonkeyPatch, **backends: Fake) -> None:
    import nexus.bench as bench

    monkeypatch.setattr(bench, "build_backend", lambda name=None, **_kw: backends[name])


def test_bench_returns_a_row_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    rest = Fake("openrouter")
    rest.delay = 0.05
    cli = Fake("codex")
    cli.kind = "cli"
    cli.usage = {"prompt_tokens": 9, "completion_tokens": 30}
    down = Fake("openai", ready=False)
    _patch_backends(monkeypatch, openrouter=rest, codex=cli, openai=down)
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/bench",
            json={
                "targets": [
                    {"backend": "openrouter", "model": "openai/gpt-4o-mini"},
                    {"backend": "codex", "model": "gpt-5.6-sol"},
                    {"backend": "openai"},
                ],
                "prompt": "Reply with exactly: ok",
                "max_tokens": 32,
            },
        )
        pulse = client.get("/dashboard/pulse").json()
    body = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert body["ok"] is True
    assert body["started_at"].endswith("Z")
    first, second, third = body["rows"]
    assert set(first) == {
        "backend",
        "kind",
        "model",
        "ok",
        "total_ms",
        "input_tokens",
        "output_tokens",
        "tok_per_s",
        "error",
    }
    assert (first["backend"], first["kind"], first["model"]) == (
        "openrouter",
        "rest",
        "openai/gpt-4o-mini",
    )
    assert first["ok"] is True
    assert first["input_tokens"] == 12
    assert first["output_tokens"] == 40
    assert first["total_ms"] >= 40
    assert first["tok_per_s"] == round(40 / (first["total_ms"] / 1000), 1)
    assert first["error"] is None
    assert rest.calls[0]["max_tokens"] == 32
    assert rest.calls[0]["stream"] is False
    assert rest.calls[0]["messages"] == [{"role": "user", "content": "Reply with exactly: ok"}]
    assert (second["backend"], second["kind"], second["ok"]) == ("codex", "cli", True)
    assert second["output_tokens"] == 30
    assert second["tok_per_s"] is None
    assert (third["backend"], third["model"], third["ok"]) == ("openai", "gpt-4o-mini", False)
    assert third["error"] == {"code": "backend_not_ready", "hint": "fix it"}
    assert third["total_ms"] == 0
    assert down.calls == []
    assert "Reply with exactly" not in response.text
    assert pulse["entries"] == []


def test_bench_targets_run_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    class Ordered(Fake):
        async def open_chat(self, payload: dict[str, Any], *, stream: bool):
            order.append(self.id)
            return await super().open_chat(payload, stream=stream)

    _patch_backends(monkeypatch, openrouter=Ordered("openrouter"), openai=Ordered("openai"))
    with TestClient(create_app()) as client:
        client.post(
            "/dashboard/bench",
            json={
                "targets": [
                    {"backend": "openai"},
                    {"backend": "openrouter"},
                    {"backend": "openai"},
                ],
                "prompt": "hi",
            },
        )
    assert order == ["openai", "openrouter", "openai"]


def test_bench_target_without_a_backend_uses_the_active_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake("openrouter")
    with TestClient(create_app(backend=fake)) as client:
        rows = client.post("/dashboard/bench", json={"targets": [{}], "prompt": "hi"}).json()[
            "rows"
        ]
    assert rows[0]["backend"] == "openrouter"
    assert rows[0]["model"] == "google/gemma-4-31b-it:free"
    assert rows[0]["ok"] is True
    assert fake.calls


def test_a_failed_target_does_not_fail_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = Fake("openrouter")
    bad.fail = GatewayError(
        502,
        "OpenRouter upstream error",
        "upstream_error",
        "openrouter_upstream_error",
        extra={"upstream_body": "sk-or-v1-leakyleakyleaky0123"},
    )
    good = Fake("openai")
    empty = Fake("xai")
    empty.text = "   "
    _patch_backends(monkeypatch, openrouter=bad, openai=good, xai=empty)
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/bench",
            json={
                "targets": [{"backend": "openrouter"}, {"backend": "xai"}, {"backend": "openai"}],
                "prompt": "hi",
            },
        )
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert rows[0]["ok"] is False
    assert rows[0]["error"]["code"] == "openrouter_upstream_error"
    assert rows[0]["output_tokens"] is None
    assert rows[0]["tok_per_s"] is None
    assert rows[1]["error"]["code"] == "bench_empty"
    assert rows[2]["ok"] is True
    assert "leakyleaky" not in response.text


def test_a_slow_target_times_out_with_a_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    slow = Fake("openrouter")
    slow.delay = 5
    _patch_backends(monkeypatch, openrouter=slow)
    with TestClient(create_app()) as client:
        rows = client.post(
            "/dashboard/bench",
            json={"targets": [{"backend": "openrouter"}], "prompt": "hi", "timeout_s": 1},
        ).json()["rows"]
    assert rows[0]["ok"] is False
    assert rows[0]["error"]["code"] == "bench_timeout"
    assert "timeout_s" in rows[0]["error"]["hint"]
    assert rows[0]["total_ms"] >= 900


def test_bench_zero_token_counts_are_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    quiet = Fake("openrouter")
    quiet.usage = {"prompt_tokens": 0, "completion_tokens": 0}
    _patch_backends(monkeypatch, openrouter=quiet)
    with TestClient(create_app()) as client:
        row = client.post(
            "/dashboard/bench", json={"targets": [{"backend": "openrouter"}], "prompt": "hi"}
        ).json()["rows"][0]
    assert row["ok"] is True
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["tok_per_s"] is None


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"prompt": "hi"}, 400, "invalid_request"),
        ({"targets": [], "prompt": "hi"}, 400, "invalid_request"),
        ({"targets": [{"backend": "openai"}]}, 400, "invalid_request"),
        ({"targets": [{"backend": "openai"}], "prompt": "  "}, 400, "invalid_request"),
        ({"targets": ["openai"], "prompt": "hi"}, 400, "invalid_request"),
        ({"targets": [{"backend": "nope"}], "prompt": "hi"}, 400, "unknown_backend"),
        (
            {"targets": [{"backend": "openai", "model": "bad model"}], "prompt": "hi"},
            400,
            "invalid_request",
        ),
        ({"targets": [{"backend": "openai"}] * 7, "prompt": "hi"}, 400, "bench_limits"),
        ({"targets": [{"backend": "openai"}], "prompt": "x" * 2001}, 400, "bench_limits"),
        (
            {"targets": [{"backend": "openai"}], "prompt": "hi", "max_tokens": 257},
            400,
            "bench_limits",
        ),
        (
            {"targets": [{"backend": "openai"}], "prompt": "hi", "max_tokens": 0},
            400,
            "bench_limits",
        ),
        (
            {"targets": [{"backend": "openai"}], "prompt": "hi", "max_tokens": "9"},
            400,
            "invalid_request",
        ),
        (
            {"targets": [{"backend": "openai"}], "prompt": "hi", "timeout_s": 61},
            400,
            "bench_limits",
        ),
        (
            {"targets": [{"backend": "openai"}], "prompt": "hi", "timeout_s": True},
            400,
            "invalid_request",
        ),
    ],
)
def test_bench_rejects_bad_requests_before_calling_anything(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], status: int, code: str
) -> None:
    fake = Fake("openai")
    _patch_backends(monkeypatch, openai=fake)
    with TestClient(create_app()) as client:
        response = client.post("/dashboard/bench", json=payload)
    assert response.status_code == status
    _assert_envelope(response.json(), code)
    assert fake.calls == []


def test_bench_accepts_the_documented_maximums(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake("openai")
    _patch_backends(monkeypatch, openai=fake)
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/bench",
            json={
                "targets": [{"backend": "openai"}] * 6,
                "prompt": "x" * 2000,
                "max_tokens": 256,
                "timeout_s": 60,
            },
        )
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 6


def test_bench_body_must_be_json() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/bench", content=b"nope", headers={"content-type": "application/json"}
        )
    assert response.status_code == 400
    _assert_envelope(response.json(), "invalid_json")


def test_bench_is_guarded_like_every_write() -> None:
    with TestClient(create_app()) as client:
        foreign = client.post(
            "/dashboard/bench",
            json={"targets": [{}], "prompt": "hi"},
            headers={"Origin": "https://evil.example"},
        )
        text = client.post(
            "/dashboard/bench",
            content='{"targets":[{}],"prompt":"hi"}',
            headers={"Content-Type": "text/plain"},
        )
        read = client.get("/dashboard/bench")
    assert foreign.status_code == 403
    assert text.status_code == 403
    assert read.status_code == 405


def test_a_second_bench_while_one_runs_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    slow = Fake("openrouter")
    slow.delay = 0.4
    _patch_backends(monkeypatch, openrouter=slow)
    app = create_app()
    body = {"targets": [{"backend": "openrouter"}], "prompt": "hi"}

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435") as c:
            first = asyncio.create_task(c.post("/dashboard/bench", json=body))
            await asyncio.sleep(0.1)
            second = await c.post("/dashboard/bench", json=body)
            done = await first
            third = await c.post("/dashboard/bench", json=body)
            return done, second, third

    done, second, third = asyncio.run(scenario())
    assert done.status_code == 200
    assert second.status_code == 409
    _assert_envelope(second.json(), "bench_busy")
    assert third.status_code == 200
    assert len(slow.calls) == 2


def test_bench_failure_releases_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.app as app_module

    async def explode(*_a: Any, **_k: Any) -> dict:
        raise RuntimeError("boom")

    app = create_app()
    monkeypatch.setattr(app_module, "run_bench", explode)
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post("/dashboard/bench", json={"targets": [{}], "prompt": "hi"})
    assert first.status_code == 500
    assert app.state.bench_running is False


def test_a_key_written_from_the_ui_is_0600_and_never_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "keys" / "xai.key"
    monkeypatch.setenv("OS3_BRIDGE_XAI_KEY_FILE", str(key_file))
    typed = "xai-" + "k" * 40
    with TestClient(create_app()) as client:
        saved = client.post("/dashboard/config", json={"backend": "xai", "api_key": typed})
        config = client.get("/dashboard/config")
        health = client.get("/health")
        doctor = client.get("/doctor")
    assert saved.status_code == 200
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    for response in (saved, config, health, doctor):
        assert typed not in response.text
    assert config.json()["keys"]["xai"]["masked"] == "\u2022\u2022\u2022\u2022kkkk"


def test_bench_stops_between_targets_when_the_client_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.bench import parse_request, run_bench

    fake = Fake("openrouter")
    _patch_backends(monkeypatch, openrouter=fake)
    request = parse_request(
        {"targets": [{"backend": "openrouter"}] * 4, "prompt": "hi"}, active="openrouter"
    )
    seen = iter([False, False, True, True])

    async def gone() -> bool:
        return next(seen)

    result = asyncio.run(run_bench(request, gate=asyncio.Semaphore(1), cancelled=gone))
    assert len(result["rows"]) == 2
    assert len(fake.calls) == 2


def test_pulse_keeps_error_codes_for_hyphenated_backend_ids(
    bridge_key: str, openrouter_key: str
) -> None:
    """claude-code and grok-build put a hyphen in every code they raise."""
    for backend_id, code in (
        ("claude-code", "claude-code_usage_limit"),
        ("grok-build", "grok-build_login_required"),
    ):
        fake = Fake(backend_id)
        fake.fail = GatewayError(429, "limit", "rate_limit_error", code)
        with TestClient(create_app(backend=fake)) as client:
            client.post("/v1/chat/completions", headers=_auth(bridge_key), json=CHAT)
            entry = client.get("/dashboard/pulse").json()["entries"][0]
        assert entry["backend"] == backend_id
        assert entry["error_code"] == code
        assert entry["hint"]


def test_note_error_still_rejects_codes_that_are_not_identifiers() -> None:
    from contextvars import copy_context

    def run(code: str) -> object:
        note_error(code, "hint")
        return pulse_module._error_code.get()

    for bad in ("Has Space", "UPPER", "semi;colon", "x" * 65, "", "a/b"):
        assert copy_context().run(run, bad) is None
    assert copy_context().run(run, "grok-build_usage_limit") == "grok-build_usage_limit"
