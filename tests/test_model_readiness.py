"""The failure a stranger hits first: the default model id is not usable.

A valid key, a listening door, and a /health that says ok is the worst
combination, because nothing is broken until the first real request. These
tests pin that /health, doctor, and the chat error all name the model that
will actually be sent.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL
from nexus.probe import execute_probe
from nexus.status import health_payload

from tests.transport import HandlerTransport, key_ok

ROOT = Path(__file__).resolve().parent.parent


def _unreachable_model(request: httpx.Request) -> httpx.Response:
    """OpenRouter answers /models, but the default id is not in the list."""
    if request.url.path.endswith("/models"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-4o-mini",
                        "object": "model",
                        "created": 1,
                        "owned_by": "openrouter",
                    }
                ]
            },
        )
    return httpx.Response(
        404,
        json={
            "error": {
                "code": 404,
                "message": f"No endpoints found matching model '{DEFAULT_MODEL}'",
            }
        },
    )


def test_health_is_not_ready_and_names_the_model(bridge_key, openrouter_key) -> None:
    app = create_app(
        backend=OpenRouterBackend(transport=HandlerTransport(key_ok(_unreachable_model)))
    )
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["ready"] is False
    assert body["model"] == DEFAULT_MODEL
    assert DEFAULT_MODEL in body["model_hint"]
    assert "NEXUS_MODEL" in body["model_hint"]


def test_health_still_200s_with_a_key_but_an_unusable_model(bridge_key, openrouter_key) -> None:
    app = create_app(
        backend=OpenRouterBackend(transport=HandlerTransport(key_ok(_unreachable_model)))
    )
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["backends"]["openrouter"]["ready"] is True


def test_health_never_raises_when_upstream_is_down(bridge_key, openrouter_key) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(down)))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["model_ready"] is None
    assert DEFAULT_MODEL in response.json()["model_hint"]


def test_chat_error_names_the_model_it_sent(bridge_key, openrouter_key) -> None:
    app = create_app(
        backend=OpenRouterBackend(transport=HandlerTransport(key_ok(_unreachable_model)))
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 502
    error = response.json()["error"]
    assert DEFAULT_MODEL in error["message"]
    assert error["model"] == DEFAULT_MODEL
    assert "NEXUS_MODEL" in error["hint"]
    assert error["upstream"]["upstream_status"] == 404


def test_doctor_probe_reports_the_model_and_exits_nonzero(bridge_key, openrouter_key) -> None:
    app = create_app(
        backend=OpenRouterBackend(transport=HandlerTransport(key_ok(_unreachable_model)))
    )

    async def run() -> tuple[int, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:11435", trust_env=False
        ) as client:
            return await execute_probe(client, bridge_key=bridge_key)

    code, report = asyncio.run(run())
    assert code == 1
    assert "health FAIL" in report
    assert f"model={DEFAULT_MODEL}" in report
    assert "NEXUS_MODEL" in report
    assert bridge_key not in report
    assert openrouter_key not in report


def test_health_payload_is_ready_when_the_model_is_live(bridge_key, openrouter_key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": DEFAULT_MODEL,
                            "object": "model",
                            "created": 1,
                            "owned_by": "openrouter",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"object": "chat.completion", "choices": []})

    backend = OpenRouterBackend(transport=HandlerTransport(key_ok(handler)))
    payload = asyncio.run(health_payload(backend=backend))
    assert payload["ready"] is True
    assert payload["model"] == DEFAULT_MODEL
    assert payload["model_hint"] == "ok"


def test_doctor_sh_exits_nonzero_when_the_model_is_unusable(tmp_path: Path) -> None:
    """The shell is the thing a stranger actually runs, so it must say so too."""
    import os
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    env = os.environ.copy()
    env["OS3_BRIDGE_KEY_FILE"] = str(tmp_path / "bridge.key")
    env["OS3_BRIDGE_OPENROUTER_KEY_FILE"] = str(tmp_path / "missing.key")
    env["OS3_BRIDGE_DOCTOR_SKIP_SMOKE"] = "1"
    env["OS3_BRIDGE_PORT"] = str(port)
    result = subprocess.run(
        ["bash", str(ROOT / "doctor.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert f"'{DEFAULT_MODEL}'" in combined, combined
    assert "NOT READY" in combined, combined


def test_model_override_is_what_health_reports(bridge_key, openrouter_key) -> None:
    """health must report the model that will actually be sent, not a constant."""
    import os

    os.environ["OS3_BRIDGE_MODEL"] = "openai/gpt-4o-mini"
    app = create_app(
        backend=OpenRouterBackend(transport=HandlerTransport(key_ok(_unreachable_model)))
    )
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["model"] == "openai/gpt-4o-mini"
    assert body["ready"] is True
