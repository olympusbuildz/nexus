"""Client contract: auth, health, models, chat proxy. No live OpenRouter."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL

from tests.transport import HandlerTransport

COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1,
    "model": DEFAULT_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "OS3_BRIDGE_OK"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}

MODELS = {
    "data": [
        {"id": "openai/gpt-4o-mini", "object": "model", "created": 10, "owned_by": "openai"},
        {
            "id": DEFAULT_MODEL,
            "object": "model",
            "created": 11,
            "owned_by": "openrouter",
        },
    ]
}


def _app(transport: HandlerTransport | None = None):
    backend = OpenRouterBackend(transport=transport) if transport is not None else None
    return create_app(backend=backend)


def _client(transport: HandlerTransport | None = None) -> TestClient:
    return TestClient(_app(transport))


def test_health_and_doctor_without_auth(bridge_key: str, openrouter_key: str) -> None:
    with _client() as client:
        health = client.get("/health")
        doctor = client.get("/doctor")
    assert health.status_code == 200
    assert doctor.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert body["service"] == "nexus"
    assert body["backend"] == "openrouter"
    assert body["codex_required"] is False
    assert body["loopback_enforced"] is True
    assert body["bind"] == "127.0.0.1:11435"
    assert set(body["backends"]) == {
        "chatgpt",
        "openrouter",
        "openai",
        "anthropic",
        "xai",
        "nous",
        "codex",
        "claude-code",
        "copilot",
        "grok-build",
        "antigravity",
    }
    assert body["backends"]["openrouter"]["implemented"] is True
    assert body["backends"]["openrouter"]["ready"] is True
    assert body["backends"]["openrouter"]["default_model"] == DEFAULT_MODEL
    for name in ("openai", "anthropic", "xai"):
        assert body["backends"][name]["implemented"] is True
        assert body["backends"][name]["ready"] is False
        assert body["backends"][name]["key_present"] is False
    for name in ("codex", "claude-code", "copilot", "grok-build", "antigravity"):
        assert body["backends"][name]["implemented"] is False
        assert body["backends"][name]["ready"] is False
        assert body["backends"][name]["cli_present"] is False
        assert body["backends"][name]["login"] == "unchecked"
    assert body["routes"]["/v1/messages"]["implemented"] is True
    assert body["routes"]["/v1/messages"]["backend"] == "openrouter"
    assert body["routes"]["/v1/messages"]["mode"] == "translate"
    doc = doctor.json()
    assert doc["auth"] == "bearer"
    assert doc["streaming"] is True
    assert doc["codex_required"] is False
    assert "/v1/chat/completions" in doc["endpoints"]
    assert "/v1/messages" in doc["endpoints"]
    assert doc["rotate"]["bridge_key_reload"] == "per_request"
    assert doc["rotate"]["provider_key_reload"] == "per_request_for_file_restart_for_env"
    for name in ("openrouter", "openai", "anthropic", "xai", "nous"):
        assert doc["rotate"][f"{name}_key_file"]
    assert doc["bridge_key"]["present"] is True
    for payload in (health.text, doctor.text):
        assert bridge_key not in payload
        assert openrouter_key not in payload


def test_health_does_not_mint_bridge_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_path = tmp_path / "bridge.key"
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(key_path))
    with _client() as client:
        assert client.get("/health").status_code == 200
        assert client.get("/doctor").status_code == 200
    assert not key_path.exists()


def test_missing_and_wrong_bearer(bridge_key: str) -> None:
    wrong = "os3_wrong_key_not_the_real_one"
    with _client() as client:
        missing = client.get("/v1/models")
        bad_scheme = client.get("/v1/models", headers={"Authorization": "Basic abc"})
        bad = client.get("/v1/models", headers={"Authorization": f"Bearer {wrong}"})
        chat = client.post(
            "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    for response, code in (
        (missing, "missing_api_key"),
        (bad_scheme, "missing_api_key"),
        (bad, "invalid_api_key"),
        (chat, "missing_api_key"),
    ):
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["type"] == "authentication_error"
        assert body["error"]["code"] == code
        assert wrong not in response.text
        assert bridge_key not in response.text
        assert response.headers["www-authenticate"].lower().startswith("bearer")
    assert missing.json()["error"]["message"] == "Missing Bearer token"
    assert "[REDACTED]" not in missing.text


def test_bearer_scheme_is_case_insensitive(bridge_key: str) -> None:
    transport = HandlerTransport(lambda _request: httpx.Response(200, json={"data": []}))
    with _client(transport) as client:
        response = client.get("/v1/models", headers={"Authorization": f"bearer {bridge_key}"})
    assert response.status_code == 200


def test_v1_bearer_mints_key_file_mode_600(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_path = tmp_path / "bridge.key"
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(key_path))
    with _client() as client:
        naked = client.get("/v1/models")
        assert not key_path.exists()
        response = client.get("/v1/models", headers={"Authorization": "Bearer not-a-real-key"})
    assert naked.status_code == 401
    assert response.status_code == 401
    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o777 == 0o600
    minted = key_path.read_text(encoding="utf-8").strip()
    assert minted not in response.text
    assert minted not in naked.text


def test_models_shape_and_default_first(auth_header: dict[str, str], openrouter_key: str) -> None:
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=MODELS))
    with _client(transport) as client:
        response = client.get("/v1/models", headers=auth_header)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == DEFAULT_MODEL
    assert body["data"][1]["id"] == "openai/gpt-4o-mini"
    for row in body["data"]:
        assert set(row) == {"id", "object", "created", "owned_by"}
        assert row["object"] == "model"
        assert isinstance(row["created"], int)
    assert openrouter_key not in response.text
    sent = transport.requests[0]
    assert sent.headers["authorization"] == f"Bearer {openrouter_key}"
    assert str(sent.url) == "https://openrouter.ai/api/v1/models"


def test_models_fallback_when_key_missing(auth_header: dict[str, str]) -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("models must not call upstream without a key")

    transport = HandlerTransport(explode)
    with _client(transport) as client:
        response = client.get("/v1/models", headers=auth_header)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == DEFAULT_MODEL
    assert body["data"][0]["owned_by"] == "nexus"
    assert transport.requests == []


def test_models_fallback_when_upstream_down(
    auth_header: dict[str, str], openrouter_key: str
) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    transport = HandlerTransport(down)
    with _client(transport) as client:
        response = client.get("/v1/models", headers=auth_header)
    assert response.status_code == 200
    assert response.json()["data"][0]["owned_by"] == "nexus"
    assert openrouter_key not in response.text


def test_default_absent_upstream_list_is_flagged_not_silent(
    auth_header: dict[str, str], openrouter_key: str
) -> None:
    """The default model the account cannot reach must not look live."""
    live = {"data": [{"id": "openai/gpt-4o-mini", "object": "model", "created": 10}]}
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=live))
    with _client(transport) as client:
        response = client.get("/v1/models", headers=auth_header)
    assert response.status_code == 200
    body = response.json()
    rows = body["data"]
    assert rows[0]["id"] == DEFAULT_MODEL
    assert rows[1]["id"] == "openai/gpt-4o-mini"
    assert "unverified" in rows[0]["warning"]
    assert "NEXUS_MODEL" in rows[0]["warning"]
    assert body["warning"] == rows[0]["warning"]
    assert "warning" not in rows[1]
    assert openrouter_key not in response.text


def test_offline_catalog_rows_all_carry_the_caveat(
    auth_header: dict[str, str],
) -> None:
    """With no upstream list at all, every row is the bridge's own guess."""

    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("models must not call upstream without a key")

    transport = HandlerTransport(explode)
    with _client(transport) as client:
        response = client.get("/v1/models", headers=auth_header)
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["id"] == DEFAULT_MODEL
    assert "warning" in body
    for row in body["data"]:
        assert row["warning"].startswith("unverified:")
    assert "doctor.sh" in body["data"][0]["warning"]


def test_confirmed_default_carries_no_warning(
    auth_header: dict[str, str], openrouter_key: str
) -> None:
    """A default the account really has stays a clean, silent row."""
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=MODELS))
    with _client(transport) as client:
        response = client.get("/v1/models", headers=auth_header)
    body = response.json()
    assert body["data"][0]["id"] == DEFAULT_MODEL
    assert "warning" not in body
    assert all("warning" not in row for row in body["data"])


def test_chat_proxy_forwards_body_and_hides_keys(
    auth_header: dict[str, str],
    bridge_key: str,
    openrouter_key: str,
) -> None:
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=COMPLETION))
    payload = {
        "messages": [{"role": "user", "content": "Reply with exactly: OS3_BRIDGE_OK"}],
        "tools": [{"type": "function", "function": {"name": "ping"}}],
        "stream": False,
    }
    with _client(transport) as client:
        response = client.post("/v1/chat/completions", headers=auth_header, json=payload)
    assert response.status_code == 200
    assert response.json() == COMPLETION
    sent = json.loads(transport.requests[0].content.decode())
    assert sent["model"] == DEFAULT_MODEL
    assert sent["tools"] == payload["tools"]
    assert sent["stream"] is False
    authorization = transport.requests[0].headers["authorization"]
    assert authorization == f"Bearer {openrouter_key}"
    assert transport.requests[0].headers["http-referer"] == "http://127.0.0.1:11435"
    assert bridge_key not in authorization
    assert bridge_key not in response.text
    assert openrouter_key not in response.text


def test_chat_defaults_model_and_rejects_bad_body(
    auth_header: dict[str, str],
    openrouter_key: str,
) -> None:
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=COMPLETION))
    with _client(transport) as client:
        ok = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        invalid = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            content=b"{",
        )
        missing = client.post("/v1/chat/completions", headers=auth_header, json={})
        bad_stream = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}], "stream": "yes"},
        )
    assert ok.status_code == 200
    assert json.loads(transport.requests[0].content.decode())["model"] == DEFAULT_MODEL
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_json"
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_messages"
    assert bad_stream.status_code == 400
    assert bad_stream.json()["error"]["code"] == "invalid_stream"
    for response in (ok, invalid, missing, bad_stream):
        assert openrouter_key not in response.text


def test_missing_openrouter_key_is_structured(
    auth_header: dict[str, str],
    bridge_key: str,
) -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("chat must not call upstream without a key")

    transport = HandlerTransport(explode)
    with _client(transport) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "openrouter_key_missing"
    assert body["error"]["type"] == "openrouter_unavailable"
    assert body["error"]["openrouter"]["key_present"] is False
    assert "./nexus setup --backend openrouter" in body["error"]["hint"]
    assert "bridge failed" not in body["error"]["hint"]
    assert "key_file" in body["error"]["openrouter"]
    assert bridge_key not in response.text
    assert "sk-" not in response.text
    assert transport.requests == []


def test_upstream_error_and_redirect_redact_secrets(
    auth_header: dict[str, str],
    openrouter_key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["messages"][0]["content"] == "redirect":
            return httpx.Response(
                302,
                headers={"Location": f"https://evil.example/steal?api_key={openrouter_key}"},
            )
        return httpx.Response(
            401,
            json={"error": {"message": f"rejected {openrouter_key}"}},
        )

    transport = HandlerTransport(handler)
    with _client(transport) as client:
        leaked = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "nope"}]},
        )
        redirect = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "redirect"}]},
        )
    assert leaked.status_code == 502
    assert leaked.json()["error"]["code"] == "openrouter_upstream_error"
    assert openrouter_key not in leaked.text
    assert "[REDACTED]" in leaked.text
    assert redirect.status_code == 502
    assert redirect.json()["error"]["code"] == "openrouter_redirect"
    assert openrouter_key not in redirect.text
    assert "evil.example" not in redirect.text
    assert len(transport.requests) == 2
    assert openrouter_key not in caplog.text


def test_success_body_redacts_upstream_key(
    auth_header: dict[str, str],
    openrouter_key: str,
) -> None:
    echoed = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"key was {openrouter_key}"},
                "finish_reason": "stop",
            }
        ],
    }
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=echoed))
    with _client(transport) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert openrouter_key not in response.text
    assert response.json()["choices"][0]["message"]["content"] == "key was [REDACTED]"


def test_stream_proxies_sse_and_redacts(
    auth_header: dict[str, str],
    openrouter_key: str,
) -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"OS3_BRIDGE_OK '
        + openrouter_key
        + '"}}]}\n\ndata: [DONE]\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse,
        )

    transport = HandlerTransport(handler)
    with _client(transport) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "OS3_BRIDGE_OK" in response.text
    assert "data: [DONE]" in response.text
    assert openrouter_key not in response.text
    assert "[REDACTED]" in response.text


def test_unreachable_is_structured(auth_header: dict[str, str], openrouter_key: str) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    transport = HandlerTransport(down)
    with _client(transport) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "openrouter_unreachable"
    assert openrouter_key not in response.text


def test_unknown_v1_endpoint_requires_auth(auth_header: dict[str, str]) -> None:
    with _client() as client:
        hidden = client.get("/v1/embeddings")
        known = client.get("/v1/embeddings", headers=auth_header)
    assert hidden.status_code == 401
    assert known.status_code == 404
    assert known.json()["error"]["code"] == "unknown_endpoint"


def test_codex_without_cli_is_not_implemented(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    with _client() as client:
        health = client.get("/health")
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        models = client.get("/v1/models", headers=auth_header)
        messages = client.post(
            "/v1/messages",
            headers=auth_header,
            json={"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
        )
    body = health.json()
    assert body["backend"] == "codex"
    assert body["codex_required"] is False
    assert body["backends"]["codex"]["implemented"] is False
    assert body["backends"]["codex"]["ready"] is False
    assert body["backends"]["codex"]["cli_present"] is False
    assert body["routes"]["/v1/messages"]["implemented"] is True
    assert body["routes"]["/v1/messages"]["mode"] == "translate"
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "codex_cli_missing"
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "gpt-5.6-sol"
    assert models.json()["data"][0]["owned_by"] == "codex"
    assert messages.status_code == 503
    assert messages.json()["type"] == "error"
    assert "CLI" in messages.json()["error"]["message"]


def test_grok_alias_is_implemented_xai_and_needs_a_key(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "grok")
    with _client() as client:
        health = client.get("/health").json()
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert health["backend"] == "xai"
    assert health["backends"]["xai"]["implemented"] is True
    assert health["backends"]["xai"]["ready"] is False
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "xai_key_missing"


def test_bridge_key_rotates_without_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    auth_header: dict[str, str],
) -> None:
    del auth_header
    path = tmp_path / "bridge.key"
    path.write_text("os3_test_bridge_key\n", encoding="utf-8")
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(path))
    with _client() as client:
        old = client.get("/v1/models", headers={"Authorization": "Bearer os3_test_bridge_key"})
        path.write_text("os3_rotated_bridge_key\n", encoding="utf-8")
        stale = client.get("/v1/models", headers={"Authorization": "Bearer os3_test_bridge_key"})
        fresh = client.get("/v1/models", headers={"Authorization": "Bearer os3_rotated_bridge_key"})
    assert old.status_code == 200
    assert stale.status_code == 401
    assert fresh.status_code == 200


def test_respx_chat_happy_path(openrouter_key: str) -> None:
    """respx covers the OpenRouter URL the backend actually calls."""
    import asyncio

    import respx
    from nexus.backends.openrouter import OpenRouterBackend

    async def call() -> bytes:
        upstream = await OpenRouterBackend().open_chat(
            {
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            stream=False,
        )
        assert upstream.body is not None
        return upstream.body

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=COMPLETION)
        )
        body = asyncio.run(call())
    assert route.calls[0].request.headers["authorization"] == f"Bearer {openrouter_key}"
    assert json.loads(body)["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert openrouter_key.encode() not in body
