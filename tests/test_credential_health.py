"""/health must prove the key works, not that a key file exists.

OpenRouter serves its /models catalog to any bearer, so a dead key still listed
the default model and /health said ready while every chat returned 401. These
tests pin the credential-bound check, that it stays out of the way of role-aware
readiness, and that it never spends more upstream calls than it has to.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.anthropic import AnthropicBackend
from nexus.backends.openai import OpenAIBackend
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL

from tests.transport import HandlerTransport

WORKER = "vendor/worker-model:free"


def _models(*ids: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in ids]},
    )


def _refused() -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "User not found.", "code": 401}})


def _openrouter(key_status: int | None, *, models: tuple[str, ...] = (DEFAULT_MODEL,)):
    """An OpenRouter whose /models is public and whose /key answers ``key_status``."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/key"):
            if key_status is None:
                raise httpx.ConnectError("down")
            if key_status == 401:
                return _refused()
            return httpx.Response(key_status, json={"data": {"label": "test"}})
        return _models(*models)

    return OpenRouterBackend(transport=HandlerTransport(handler)), calls


def _health(backend) -> dict:
    with TestClient(create_app(backend=backend)) as client:
        return client.get("/health").json()


def test_dead_openrouter_key_is_not_ready_even_though_models_lists(
    bridge_key, openrouter_key
) -> None:
    backend, _ = _openrouter(401)
    app = create_app(backend=backend)
    with TestClient(app) as client:
        health = client.get("/health").json()
        doctor = client.get("/doctor").json()
    assert health["ready"] is False
    assert health["model_ready"] is False
    assert health["upstream_status"] == 401
    assert "rejected the API key" in health["model_hint"]
    assert "User not found" in health["model_hint"]
    assert openrouter_key not in str(health)
    assert doctor["ready"] is False
    assert doctor["upstream_status"] == 401


def test_live_openrouter_key_is_ready(bridge_key, openrouter_key) -> None:
    backend, calls = _openrouter(200)
    body = _health(backend)
    assert body["ready"] is True
    assert body["model_hint"] == "ok"
    assert "upstream_status" not in body
    assert any(c.endswith("/key") for c in calls)


def test_unreachable_credential_check_is_unverified_not_ready(bridge_key, openrouter_key) -> None:
    backend, _ = _openrouter(None)
    body = _health(backend)
    assert body["model_ready"] is None
    assert body["ready"] is False
    assert DEFAULT_MODEL in body["model_hint"]


def test_credential_is_asked_once_for_every_role_model(
    monkeypatch, bridge_key, openrouter_key
) -> None:
    """Role-aware readiness is kept: a dead key fails every model, one /key call."""
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", WORKER)
    backend, calls = _openrouter(401, models=(DEFAULT_MODEL, WORKER))
    body = _health(backend)
    assert body["ready"] is False
    assert body["roles"]["worker"]["ready"] is False
    assert "rejected the API key" in body["roles"]["worker"]["hint"]
    assert body["roles_ready"] is False
    assert sum(1 for c in calls if c.endswith("/key")) == 1


def test_live_key_with_a_gone_role_model_still_names_the_role_setting(
    monkeypatch, bridge_key, openrouter_key
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_WORKER", "vendor/gone")
    backend, _ = _openrouter(200)
    body = _health(backend)
    assert body["model_ready"] is True
    assert body["roles"]["worker"]["ready"] is False
    assert "NEXUS_MODEL_WORKER" in body["roles_hint"]
    assert body["ready"] is False


def test_models_backend_reports_a_refused_key_not_an_unconfirmed_model(
    monkeypatch, bridge_key
) -> None:
    """Providers whose /models is authenticated say so on a 401, in one models call."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openai")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided."}})

    body = _health(OpenAIBackend(transport=HandlerTransport(handler)))
    assert body["ready"] is False
    assert body["upstream_status"] == 401
    assert "rejected the API key" in body["model_hint"]
    assert "sk-test-secret-value" not in str(body)


def test_models_backend_with_a_live_key_asks_models_once(monkeypatch, bridge_key) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openai")
    model = "gpt-4o-mini"
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _models(model)

    body = _health(OpenAIBackend(transport=HandlerTransport(handler)))
    assert body["model_ready"] is True
    assert len(paths) == 1


def test_anthropic_refused_key_is_not_ready(monkeypatch, bridge_key) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    body = _health(AnthropicBackend(transport=HandlerTransport(handler)))
    assert body["ready"] is False
    assert body["upstream_status"] == 401
    assert "invalid x-api-key" in body["model_hint"]
    assert "sk-ant-test-secret-value" not in str(body)
