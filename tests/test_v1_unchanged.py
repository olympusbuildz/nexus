"""The /v1 surface Rabbit OS3 uses must stay byte-for-byte what it was.

``fixtures/v1_golden.json`` was captured from ``main`` before the Nexus UI API
landed: status, content type, auth challenge, and raw body for each case below.
The Nexus work only adds routes outside ``/v1``; if one of these bytes moves, a
control-plane change leaked into the OpenAI door. Regenerate the file only for a
deliberate /v1 change, with ``OS3_BRIDGE_REGEN_V1_GOLDEN=1``.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_HOST, DEFAULT_MODEL, DEFAULT_PORT
from nexus.status import ENDPOINTS

from tests.transport import HandlerTransport

GOLDEN = Path(__file__).parent / "fixtures" / "v1_golden.json"

COMPLETION = {
    "id": "chatcmpl-golden",
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
        {"id": DEFAULT_MODEL, "object": "model", "created": 11, "owned_by": "openrouter"},
    ]
}
STREAM = (
    b'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
    b'"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    b"data: [DONE]\n\n"
)


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/key"):
        return httpx.Response(200, json={"data": {"label": "golden"}})
    if path.endswith("/models"):
        return httpx.Response(200, json=MODELS)
    body = json.loads(request.content or b"{}")
    if body.get("model") == "upstream/boom":
        return httpx.Response(500, json={"error": {"message": "boom"}})
    if body.get("stream"):
        return httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json=COMPLETION)


def _record(response: httpx.Response) -> dict:
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "www_authenticate": response.headers.get("www-authenticate"),
        "body_b64": base64.b64encode(response.content).decode("ascii"),
    }


def capture(bridge_key: str) -> dict:
    good = {"Authorization": f"Bearer {bridge_key}"}
    chat = {"messages": [{"role": "user", "content": "hi"}]}
    backend = OpenRouterBackend(transport=HandlerTransport(_handler))
    cases: dict[str, dict] = {}
    with TestClient(create_app(backend=backend)) as client:
        cases["models_no_auth"] = _record(client.get("/v1/models"))
        cases["models_bad_key"] = _record(
            client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        )
        cases["models_ok"] = _record(client.get("/v1/models", headers=good))
        cases["chat_ok"] = _record(
            client.post("/v1/chat/completions", headers=good, json={**chat, "model": "m"})
        )
        cases["chat_stream"] = _record(
            client.post(
                "/v1/chat/completions", headers=good, json={**chat, "model": "m", "stream": True}
            )
        )
        cases["chat_bad_json"] = _record(
            client.post(
                "/v1/chat/completions",
                headers={**good, "content-type": "application/json"},
                content=b"{not json",
            )
        )
        cases["chat_bad_messages"] = _record(
            client.post("/v1/chat/completions", headers=good, json={"model": "m", "messages": 3})
        )
        cases["chat_upstream_error"] = _record(
            client.post(
                "/v1/chat/completions", headers=good, json={**chat, "model": "upstream/boom"}
            )
        )
        cases["messages_ok"] = _record(
            client.post(
                "/v1/messages",
                headers=good,
                json={
                    "model": "m",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        cases["unknown_endpoint"] = _record(client.get("/v1/nope", headers=good))
        cases["v1_root"] = _record(client.get("/v1", headers=good))
        cases["unknown_no_auth"] = _record(client.post("/v1/whatever"))
    return cases


def test_v1_bytes_match_the_pre_nexus_golden(bridge_key: str, openrouter_key: str) -> None:
    got = capture(bridge_key)
    if os.environ.get("OS3_BRIDGE_REGEN_V1_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(got) == set(expected)
    for name, record in got.items():
        assert record == expected[name], name


def test_v1_does_not_pass_through_the_dashboard_guard(bridge_key: str) -> None:
    """/v1 is bearer-only: a foreign Host or Origin is not the guard's business."""
    with TestClient(create_app(backend=None)) as client:
        response = client.get(
            "/v1/models", headers={"Host": "evil.example", "Origin": "https://evil.example"}
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_bind_and_advertised_endpoints_are_unchanged() -> None:
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 11435
    assert ENDPOINTS == [
        "/health",
        "/doctor",
        "/v1/models",
        "/v1/chat/completions",
        "/v1/messages",
    ]
