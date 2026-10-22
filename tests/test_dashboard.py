"""Same-origin dashboard. Never prints a key."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.config import private_mode

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def test_dashboard_is_same_origin_and_has_no_key() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/")
        health = client.get("/health")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert 'id="key"' in page.text
    assert 'value=""' in page.text
    assert "os3_" not in page.text
    assert "sk-" not in page.text
    assert health.status_code == 200
    assert health.json()["ok"] is True


def test_dashboard_writes_choice_and_key_without_echo(tmp_path, monkeypatch) -> None:
    choice = tmp_path / "backend-choice"
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(choice))
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    typed = "sk-proj-dashboard-test-key"
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": typed, "model": "gpt-4o-mini"},
        )
        page = client.get("/")
        health = client.get("/health")
    assert saved.status_code == 200
    body = saved.json()
    assert body["ok"] is True
    assert body["backend"] == "openai"
    assert body["key_written"] == "yes"
    assert typed not in saved.text
    assert typed not in page.text
    assert choice.read_text(encoding="utf-8").strip() == "openai"
    assert key_path.read_text(encoding="utf-8").strip() == typed
    assert private_mode(key_path) == "600"
    assert health.json()["backend"] == "openai"
    assert health.json()["backends"]["openai"]["key_present"] is True


def test_dashboard_folds_cli_models_into_the_models_column() -> None:
    """CLI resolution lives in Models. Long catalogs collapse to 3 + N more."""
    with TestClient(create_app()) as client:
        page = client.get("/")
    assert "<th>CLI model</th>" not in page.text
    assert "<th>Models</th>" in page.text
    assert "CLI model not reported" in page.text
    assert 'limit + " + " + extra + " more"' in page.text
    assert "are not reported" not in page.text
    assert "modelDirty" in page.text


def test_dashboard_saves_a_model_for_the_chosen_backend(tmp_path, monkeypatch) -> None:
    choice = tmp_path / "backend-choice"
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(choice))
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    chosen = "openai/gpt-4o"
    with TestClient(create_app()) as client:
        page = client.get("/")
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-dashboard-model", "model": chosen},
        )
        health = client.get("/health")
    assert 'id="model-list"' in page.text
    assert "readonly" not in page.text.split('id="model"')[1].split(">")[0]
    body = saved.json()
    assert saved.status_code == 200
    assert body["ok"] is True
    assert body["model"] == chosen
    assert "Model saved" in body["model_note"]
    model_file = tmp_path / "model-choice"
    assert model_file.read_text(encoding="utf-8").strip() == chosen
    assert health.json()["model"] == chosen
    assert health.json()["backends"]["openai"]["default_model"] == chosen
    assert health.json()["backends"]["anthropic"]["default_model"] == "claude-sonnet-4-5"


def test_dashboard_rejects_a_model_id_with_spaces(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-dashboard-model", "model": "two words"},
        )
    assert saved.status_code == 400
    assert "Model must be one id" in saved.json()["error"]
    assert not Path(os.environ["OS3_BRIDGE_MODEL_FILE"]).exists()


def test_environment_model_wins_over_the_saved_choice(tmp_path, monkeypatch) -> None:
    from nexus.config import model_for

    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_MODEL", "gpt-4o-mini")
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-dashboard-model", "model": "gpt-4o"},
        )
    assert saved.status_code == 200
    assert "still wins" in saved.json()["model_note"]
    assert model_for("openai") == "gpt-4o-mini"


def test_blank_model_uses_the_backend_default(tmp_path, monkeypatch) -> None:
    from nexus.config import model_for

    choice = tmp_path / "backend-choice"
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(choice))
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    with TestClient(create_app()) as client:
        first = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-dashboard-model", "model": "gpt-4o"},
        )
        cleared = client.post("/dashboard/config", json={"backend": "openai", "model": ""})
    assert first.status_code == 200
    assert cleared.status_code == 200
    assert cleared.json()["model"] == ""
    assert model_for("openai") == "gpt-4o-mini"


def test_dashboard_refuses_to_echo_a_cli_key() -> None:
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={"backend": "codex", "api_key": "sk-proj-not-for-cli"},
        )
    assert saved.status_code == 400
    assert "sk-proj-not-for-cli" not in saved.text
    assert saved.json()["ok"] is False


def test_dashboard_model_picker_lists_every_backend_catalog() -> None:
    """The page offers a real select of info.models, including subscription catalogs."""
    with TestClient(create_app()) as client:
        page = client.get("/")
        health = client.get("/health")
    assert 'id="model-pick"' in page.text
    assert "info.models" in page.text
    assert "googleapis" not in page.text
    backends = health.json()["backends"]
    for name in ("codex", "claude-code", "copilot", "grok-build", "antigravity", "openai"):
        models = backends[name]["models"]
        assert models
        assert backends[name]["default_model"] in models
    assert len(backends["claude-code"]["models"]) > 1
    assert len(backends["copilot"]["models"]) > 1
    assert len(backends["codex"]["models"]) > 1


def test_dashboard_fonts_are_local_woff2() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/")
        font = client.get("/dashboard/fonts/syne.woff2")
        mono = client.get("/dashboard/fonts/plex-mono-400.woff2")
        missing = client.get("/dashboard/fonts/not-a-font.woff2")
    assert "/dashboard/fonts/syne.woff2" in page.text
    assert font.status_code == 200
    assert font.content[:4] == b"wOF2"
    assert font.headers["content-type"].startswith("font/woff2")
    assert mono.status_code == 200
    assert mono.content[:4] == b"wOF2"
    assert missing.status_code == 404


def test_dashboard_wiring_is_in_the_page_without_key_material() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/")
    text = page.text
    assert page.status_code == 200
    assert 'id="connection"' in text
    assert "window.location.host" in text
    assert 'id="copy-url"' in text
    assert 'id="copy-key"' in text
    assert 'id="active-model"' in text
    assert "cannot restart" in text
    assert "os3_" not in text
    assert "sk-" not in text
    assert 'value=""' in text


def test_bridge_key_copy_returns_only_the_bridge_key(bridge_key: str, openrouter_key: str) -> None:
    with TestClient(create_app()) as client:
        copied = client.get("/dashboard/bridge-key", headers=SAME_ORIGIN)
        page = client.get("/")
        health = client.get("/health")
        doctor = client.get("/doctor")
        pulse = client.get("/dashboard/pulse")
    assert copied.status_code == 200
    assert copied.headers["cache-control"] == "no-store"
    body = copied.json()
    assert body == {"present": True, "key": bridge_key}
    for payload in (page.text, health.text, doctor.text, pulse.text):
        assert bridge_key not in payload
        assert openrouter_key not in payload


def test_bridge_key_copy_does_not_mint(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bridge.key"
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(path))
    with TestClient(create_app()) as client:
        copied = client.get("/dashboard/bridge-key", headers=SAME_ORIGIN)
    assert copied.status_code == 200
    assert copied.json() == {"present": False}
    assert not path.exists()


def test_save_says_a_foreground_door_cannot_restart_itself(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_SERVICE", raising=False)
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={
                "backend": "openai",
                "api_key": "sk-proj-dashboard-model",
                "model": "gpt-4o-mini",
            },
        )
        health = client.get("/health")
    body = saved.json()
    assert saved.status_code == 200
    assert body["ok"] is True
    assert body["restart_available"] == "no"
    assert body["restart_managed"] == "no"
    assert body["restart_command"] == ""
    assert "serving this backend now" in body["restart"]
    assert health.status_code == 200
    assert health.json()["restart_available"] is False
    assert health.json()["backend"] == "openai"
    assert health.json()["backend_source"] == "file"
    assert health.json()["model"] == "gpt-4o-mini"


def test_packaged_door_offers_stop_then_start_without_running_it(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_SERVICE", "1")
    with TestClient(create_app()) as client:
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-dashboard-model", "model": "gpt-4o"},
        )
        still_up = client.get("/health")
    body = saved.json()
    assert body["restart_available"] == "no"
    assert body["restart_managed"] == "yes"
    assert body["restart_command"] == ""
    assert "serving this backend now" in body["restart"]
    assert still_up.status_code == 200
    assert still_up.json()["backend"] == "openai"
    assert still_up.json()["backend_source"] == "file"
    assert still_up.json()["restart_managed"] is True
    assert still_up.json()["restart_command"] == "./nexus stop && ./nexus start"


def test_saved_backend_beats_the_env_pin_without_a_restart(tmp_path, monkeypatch) -> None:
    """LaunchAgent-style OS3_BRIDGE_BACKEND=nous must not hide a Settings save."""
    key_path = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key_path))
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    monkeypatch.setenv("OS3_BRIDGE_SERVICE", "1")
    with TestClient(create_app()) as client:
        before = client.get("/health").json()
        saved = client.post(
            "/dashboard/config",
            json={"backend": "openai", "api_key": "sk-proj-dashboard-model"},
        )
        health = client.get("/health").json()
        doctor = client.get("/doctor").json()
    assert before["backend"] == "nous"
    assert before["backend_source"] == "env"
    assert saved.status_code == 200
    assert saved.json()["backend"] == "openai"
    assert "serving this backend now" in saved.json()["restart"]
    assert saved.json()["restart_command"] == ""
    assert health["backend"] == "openai"
    assert health["backend_source"] == "file"
    assert doctor["backend"] == "openai"
    assert doctor["backend_source"] == "file"


def test_refuses_to_activate_a_backend_that_is_not_ready(tmp_path, monkeypatch) -> None:
    """A live /v1 client must keep the old backend when the new one cannot serve."""
    from nexus.backends.subscription import clear_probe_cache

    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    model = Path(os.environ["OS3_BRIDGE_MODEL_FILE"])
    choice.write_bytes(b"nous\n")
    model.write_bytes(b"meituan/longcat-2.5-preview:free\n")
    before_choice = choice.read_bytes()
    before_model = model.read_bytes()
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\necho 'not logged in'\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(binary))
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    clear_probe_cache()
    with TestClient(create_app()) as client:
        refused = client.post("/dashboard/config", json={"backend": "codex"})
        health = client.get("/health").json()
    assert refused.status_code == 409
    body = refused.json()
    assert body["ok"] is False
    assert body["code"] == "not_ready"
    assert "codex login" in body["error"]
    assert body["hint"]
    assert "not logged in" in body["error"].lower() or "logged in" in body["error"].lower()
    assert choice.read_bytes() == before_choice
    assert model.read_bytes() == before_model
    assert health["backend"] == "nous"
    assert health["backend_source"] == "file"


def test_a_backend_switch_drops_a_model_the_new_backend_cannot_serve(tmp_path, monkeypatch) -> None:
    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    model = Path(os.environ["OS3_BRIDGE_MODEL_FILE"])
    choice.write_text("openai\n", encoding="utf-8")
    model.write_text("gpt-4o\n", encoding="utf-8")
    key = tmp_path / "openrouter.key"
    key.write_text("sk-or-switch-model-0123456789\n", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_OPENROUTER_KEY_FILE", str(key))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    with TestClient(create_app()) as client:
        saved = client.post("/dashboard/config", json={"backend": "openrouter"})
        health = client.get("/health").json()
    assert saved.status_code == 200
    assert "Reset the model" in saved.json()["model_note"]
    assert "gpt-4o" in saved.json()["model_note"]
    assert not model.exists()
    assert health["backend"] == "openrouter"
    assert health["model"] != "gpt-4o"


def test_posting_the_old_model_does_not_carry_it_to_the_new_backend(tmp_path, monkeypatch) -> None:
    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    model = Path(os.environ["OS3_BRIDGE_MODEL_FILE"])
    choice.write_text("openai\n", encoding="utf-8")
    model.write_text("gpt-4o\n", encoding="utf-8")
    key = tmp_path / "openrouter.key"
    key.write_text("sk-or-switch-post-0123456789\n", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_OPENROUTER_KEY_FILE", str(key))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    with TestClient(create_app()) as client:
        saved = client.post("/dashboard/config", json={"backend": "openrouter", "model": "gpt-4o"})
    assert saved.status_code == 200
    assert "Reset the model" in saved.json()["model_note"]
    assert saved.json()["model"] == ""
    assert not model.exists()


def test_a_backend_switch_keeps_a_model_the_new_backend_serves(tmp_path, monkeypatch) -> None:
    choice = Path(os.environ["OS3_BRIDGE_BACKEND_FILE"])
    model = Path(os.environ["OS3_BRIDGE_MODEL_FILE"])
    shared = "openai/gpt-4o-mini"
    choice.write_text("openai\n", encoding="utf-8")
    model.write_text(shared + "\n", encoding="utf-8")
    key = tmp_path / "openrouter.key"
    key.write_text("sk-or-switch-keep-0123456789\n", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_OPENROUTER_KEY_FILE", str(key))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    with TestClient(create_app()) as client:
        saved = client.post("/dashboard/config", json={"backend": "openrouter"})
        health = client.get("/health").json()
    assert saved.status_code == 200
    assert saved.json()["model_note"].startswith(f"Kept {shared}.")
    assert model.read_text(encoding="utf-8").strip() == shared
    assert health["model"] == shared


def test_nous_switch_warns_when_token_sync_fails_without_the_token(tmp_path, monkeypatch) -> None:
    from nexus.nous_token import SyncResult

    key_path = tmp_path / "nous.key"
    secret = "nous-token-must-not-appear"
    key_path.write_text(secret + "\n", encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_NOUS_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    reason = "ERROR: Hermes auth has no usable token. Run `hermes auth add nous`."
    monkeypatch.setattr(
        "nexus.app.sync_nous_token",
        lambda: SyncResult(False, reason),
    )
    with TestClient(create_app()) as client:
        saved = client.post("/dashboard/config", json={"backend": "nous"})
    assert saved.status_code == 200
    assert saved.json()["warning"] == reason
    assert secret not in saved.text


def test_switching_to_nous_starts_the_refresh_loop(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "nous.key"
    key_path.write_text("not-a-real-token\n", encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_NOUS_KEY_FILE", str(key_path))
    monkeypatch.delenv("OS3_BRIDGE_BACKEND", raising=False)
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        saved = client.post("/dashboard/config", json={"backend": "nous"})
        task = client.app.state.nous_refresh_task
        assert saved.status_code == 200
        assert saved.json()["backend"] == "nous"
        assert task is not None
        assert not task.done()


def test_pulse_caps_recent_v1_requests_without_secrets(
    bridge_key: str, openrouter_key: str
) -> None:
    import httpx
    from nexus.backends.openrouter import OpenRouterBackend

    from tests.transport import HandlerTransport

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl",
                "object": "chat.completion",
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    with TestClient(app) as client:
        for _ in range(45):
            denied = client.get("/v1/not-a-route")
            assert denied.status_code == 401
        chat = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": openrouter_key}],
            },
        )
        poisoned = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"model": openrouter_key, "messages": [{"role": "user", "content": "hi"}]},
        )
        leaked = client.get(
            f"/v1/{openrouter_key}",
            headers={"Authorization": f"Bearer {bridge_key}"},
        )
        pulse = client.get("/dashboard/pulse")
        health = client.get("/health")
        doctor = client.get("/doctor")
        page = client.get("/")
    assert chat.status_code == 200
    assert poisoned.status_code == 200
    assert leaked.status_code == 404
    body = pulse.json()
    assert body["cap"] == 40
    assert len(body["entries"]) == 40
    traffic = body["traffic"]
    assert traffic["calls"] >= 40
    assert traffic["ok"] >= 1
    assert traffic["tokens"]["known_calls"] >= 1
    assert traffic["tokens"]["total"] is not None
    assert traffic["tokens"]["total"] >= 18
    assert "plan balance" in traffic["note"].lower()
    newest, secret_model, named = body["entries"][:3]
    assert newest["route"] == "GET /v1"
    assert newest["model"] is None
    assert secret_model["route"] == "POST /v1/chat/completions"
    assert secret_model["model"] is None
    assert named["model"] == "gpt-4o-mini"
    assert named["status"] == 200
    assert named["route"] == "POST /v1/chat/completions"
    assert named["prompt_tokens"] == 11
    assert named["completion_tokens"] == 7
    required = {
        "ts",
        "route",
        "model",
        "status",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "usage_limit",
    }
    for entry in body["entries"]:
        assert required <= set(entry)
        assert set(entry) <= required | {"backend", "error_code", "hint"}
        assert isinstance(entry["status"], int)
        assert isinstance(entry["latency_ms"], int)
        assert entry["latency_ms"] >= 0
        assert "/v1" in entry["route"]
        assert not entry["route"].endswith("/health")
    blob = pulse.text + health.text + doctor.text + page.text
    assert bridge_key not in blob
    assert openrouter_key not in blob
    assert f"bearer {bridge_key}".lower() not in pulse.text.lower()
    assert 'id="traffic"' in page.text
    assert "Traffic through this door" in page.text
    assert "Not your Claude, ChatGPT, or Grok plan balance" in page.text


def test_pulse_marks_usage_limit_without_inventing_plan_quota(bridge_key: str) -> None:
    from nexus.errors import GatewayError

    class LimitingBackend:
        id = "openrouter"

        def describe(self) -> dict:
            return {"id": "openrouter", "ready": True}

        async def list_models(self) -> list:
            return []

        async def open_chat(self, payload: dict, *, stream: bool):
            raise GatewayError(
                429,
                "subscription usage limit reached",
                "rate_limit_error",
                "codex_usage_limit",
            )

    app = create_app(backend=LimitingBackend())
    with TestClient(app) as client:
        chat = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        pulse = client.get("/dashboard/pulse")
    assert chat.status_code == 429
    body = pulse.json()
    assert body["entries"][0]["usage_limit"] is True
    assert body["entries"][0]["status"] == 429
    assert body["traffic"]["usage_limit"] == 1
    assert body["traffic"]["tokens"]["total"] is None
    assert body["traffic"]["tokens"]["known_calls"] == 0


def test_dashboard_has_one_verdict_and_a_persistent_failure_banner() -> None:
    """Status, banner and Configure all read status(); the banner has no dismiss."""
    with TestClient(create_app()) as client:
        page = client.get("/").text
    assert 'id="banner"' in page
    assert 'role="alert"' in page
    assert "function status()" in page
    assert page.count("status()") >= 3
    assert 'id="running"' in page
    assert "Same as Status." in page
    assert "only a draft until you save it" in page
    assert "Service isn't responding" in page
    assert "Values shown are stale" in page
    banner = page.split('id="banner"')[1].split('<section class="hero"')[0]
    assert "<button" not in banner


def test_dashboard_backends_start_collapsed_with_a_ready_count() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/").text
    assert "let backendsOpen = false;" in page
    assert 'id="backends-toggle"' in page
    assert 'aria-expanded="false"' in page
    assert '" of " + names.length + " ready"' in page
    assert 'id="chips"' in page


def test_dashboard_labels_catalog_ids_as_guesses_not_entitlements() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/").text
    assert "Suggested models" in page or "may not be on your plan" in page
    assert "Suggested" in page
    assert "reported by CLI" in page
    assert '"unverified"' in page
    assert "Nous Portal (advanced)" in page


def test_dashboard_keeps_the_cyan_door_and_never_goes_light() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/").text
    assert "--accent: #2ee6dc;" in page
    assert "color-scheme: dark;" in page
    assert "prefers-color-scheme: light" not in page
    assert 'id="scope"' in page
    assert "conic-gradient" in page
    assert "prefers-reduced-motion" in page


def test_dashboard_models_catalog_for_a_backend_with_no_key() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/models", params={"backend": "codex"})
    body = response.json()
    assert response.status_code == 200
    assert body["backend"] == "codex"
    assert body["source"] == "catalog"
    assert body["default_model"] in {row["id"] for row in body["models"]}
    assert {row["status"] for row in body["models"]} == {"catalog"}
    assert "not the account's" in body["hint"]


def test_dashboard_models_defaults_to_the_active_backend() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/models")
    assert response.status_code == 200
    assert response.json()["backend"] == "openrouter"
    assert response.json()["source"] == "catalog"


def test_dashboard_models_rejects_an_unknown_backend() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/models", params={"backend": "no-such"})
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_dashboard_models_prefers_the_cli_list(monkeypatch) -> None:
    from nexus import dashboard_models

    class FakeCli:
        id = "grok-build"

        def describe(self) -> dict:
            return {
                "kind": "cli",
                "default_model": "grok-build",
                "models": ["grok-4.5", "grok-build"],
                "models_source": "cli",
                "models_hint": "Enumerated from `grok models`, the list the account reported.",
            }

    monkeypatch.setattr(dashboard_models, "build_backend", lambda name=None: FakeCli())
    with TestClient(create_app()) as client:
        body = client.get("/dashboard/models", params={"backend": "grok-build"}).json()
    assert body["source"] == "cli"
    assert [row["status"] for row in body["models"]] == ["cli", "cli"]


def test_dashboard_models_asks_the_provider_when_a_key_is_on_file(
    openrouter_key: str, monkeypatch
) -> None:
    import httpx
    from nexus import dashboard_models
    from nexus.backends.openrouter import OpenRouterBackend

    from tests.transport import HandlerTransport

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "acme/real-model", "object": "model"},
                    {"id": "acme/other", "object": "model"},
                    {"id": openrouter_key, "object": "model"},
                ]
            },
        )

    transport = HandlerTransport(handler)
    monkeypatch.setattr(
        dashboard_models, "build_backend", lambda name=None: OpenRouterBackend(transport=transport)
    )
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/models", params={"backend": "openrouter"})
    body = response.json()
    assert body["source"] == "live"
    ids = [row["id"] for row in body["models"]]
    assert ids[0] == "google/gemma-4-31b-it:free"
    assert body["models"][0]["status"] == "unverified"
    assert ids[1:] == ["acme/real-model", "acme/other"]
    assert {row["status"] for row in body["models"][1:]} == {"live"}
    assert openrouter_key not in response.text


def test_dashboard_models_falls_back_to_catalog_when_the_provider_is_down(
    openrouter_key: str, monkeypatch
) -> None:
    import httpx
    from nexus import dashboard_models
    from nexus.backends.openrouter import OpenRouterBackend

    from tests.transport import HandlerTransport

    transport = HandlerTransport(lambda request: httpx.Response(500, json={}))
    monkeypatch.setattr(
        dashboard_models, "build_backend", lambda name=None: OpenRouterBackend(transport=transport)
    )
    with TestClient(create_app()) as client:
        body = client.get("/dashboard/models", params={"backend": "openrouter"}).json()
    assert body["source"] == "catalog"
    assert {row["status"] for row in body["models"]} == {"catalog"}


def test_dashboard_models_refuses_a_foreign_host() -> None:
    with TestClient(create_app(), base_url="http://evil.example") as client:
        response = client.get("/dashboard/models")
    assert response.status_code == 403


def test_dashboard_uses_plain_headings_and_no_hud_leftovers() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/").text
    markup = page.split("<body")[1].split("<script>")[0]
    assert '<h2 id="calls-heading">Recent calls</h2>' in markup
    assert '<h2 id="connect-heading">Connect your app</h2>' in markup
    assert "Pulse</h2>" not in markup
    assert "Pulse is unread" not in page
    assert ">Pulse<" not in page
    assert "Connection</h2>" not in page
    assert "Loopback" not in page
    assert "Not loopback" not in page
    assert "This computer only" in page
    assert 'class="live">0' not in markup
    assert "decimal-leading-zero" not in page
    assert "counter-increment" not in page
    assert "Save</button>" in markup


def test_dashboard_keeps_machine_strings_in_mono_and_chrome_in_sans() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/").text
    css = page.split("<style>")[1].split("</style>")[0]
    for selector in ("h2", "h3", "button.primary", "button.ghost", ".chip", ".kicker"):
        block = (
            css.split("\n  " + selector + " {")[1].split("}")[0]
            if ("\n  " + selector + " {" in css)
            else ""
        )
        assert "var(--mono)" not in block, selector
    assert "line-height: 1.6;" in css
    assert ".model-id" in css and "var(--mono)" in css
