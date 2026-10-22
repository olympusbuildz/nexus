"""POST /v1/images/generations: chatgpt plan images, Responses fallback, opt-in Nous. No network."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends import chatgpt as chatgpt_backend
from nexus.backends.chatgpt import ChatGPTBackend
from nexus.usage import store

from tests.test_chatgpt import _jwt, _write_auth
from tests.transport import HandlerTransport

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


@pytest.fixture
def plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "codex-auth.json"
    _write_auth(path, _jwt(time.time() + 3600))
    monkeypatch.setenv("NEXUS_CHATGPT_AUTH_FILE", str(path))
    monkeypatch.setenv("NEXUS_BACKEND", "chatgpt")
    monkeypatch.delenv("NEXUS_IMAGES_BACKEND", raising=False)
    monkeypatch.delenv("NEXUS_ENABLE_NOUS_IMAGES", raising=False)
    chatgpt_backend._models_cache.clear()
    return path


def _client(handler: Any, backend: Any = None) -> tuple[TestClient, HandlerTransport]:
    transport = HandlerTransport(handler)
    app = create_app(backend=backend or ChatGPTBackend(transport=transport))
    app.state.images_transport = transport
    return TestClient(app), transport


def _images_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/images/generations"):
        return httpx.Response(
            200, json={"created": 1, "data": [{"b64_json": PNG, "revised_prompt": "a cat"}]}
        )
    return httpx.Response(404)


BODY = {"prompt": "a cat", "model": "gpt-image-2", "size": "1024x1024", "quality": "low"}


def test_needs_bridge_key(plan: Path, bridge_key: str) -> None:
    client, transport = _client(_images_ok)
    assert client.post("/v1/images/generations", json=BODY).status_code == 401
    bad = client.post("/v1/images/generations", json=BODY, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert not [r for r in transport.requests if "images" in r.url.path]


def test_plan_images_endpoint(plan: Path, auth_header: dict[str, str]) -> None:
    client, transport = _client(_images_ok)
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == [{"b64_json": PNG, "revised_prompt": "a cat"}]
    assert r.headers["x-nexus-image-route"] == "images"
    sent = [q for q in transport.requests if q.url.path.endswith("/images/generations")][0]
    assert sent.url.path == "/backend-api/codex/images/generations"
    assert sent.headers["originator"] == "codex_cli_rs"
    assert sent.headers["chatgpt-account-id"] == "acct-test-123"
    assert sent.headers["x-codex-image-turn-id"]
    assert sent.headers["authorization"].startswith("Bearer ")
    body = json.loads(sent.content)
    assert body == {
        "prompt": "a cat",
        "model": "gpt-image-2",
        "n": 1,
        "quality": "low",
        "size": "1024x1024",
        "background": "auto",
    }
    assert store().snapshot()["chatgpt"]["estimate"]["images_1h"] == 1
    assert store().snapshot()["chatgpt"]["images_share_plan"] is True
    assert client.get("/dashboard/pulse").json()["traffic"]["images"] == 1


def test_url_format_is_data_url(plan: Path, auth_header: dict[str, str]) -> None:
    client, _ = _client(_images_ok)
    r = client.post(
        "/v1/images/generations", json={**BODY, "response_format": "url"}, headers=auth_header
    )
    assert r.json()["data"][0]["url"] == f"data:image/png;base64,{PNG}"


def _responses_events() -> bytes:
    events = [
        {"type": "response.image_generation_call.in_progress"},
        {
            "type": "response.output_item.done",
            "item": {"type": "image_generation_call", "result": PNG, "revised_prompt": "cat"},
        },
        {"type": "response.completed", "response": {"status": "completed", "output": []}},
    ]
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_falls_back_to_responses_tool(plan: Path, auth_header: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200, content=_responses_events(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404)

    client, transport = _client(handler)
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.headers["x-nexus-image-route"] == "responses"
    assert r.json()["data"][0]["b64_json"] == PNG
    sent = json.loads(
        [q for q in transport.requests if q.url.path.endswith("/responses")][0].content
    )
    assert sent["tools"][0]["type"] == "image_generation"
    assert sent["tool_choice"] == {"type": "image_generation"}


def test_quota_is_429(plan: Path, auth_header: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "usage_limit_reached"}})

    client, _ = _client(handler)
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "chatgpt_quota"
    assert "acct-test-123" not in r.text


@pytest.mark.parametrize(
    "bad",
    [
        {"prompt": ""},
        {"prompt": "x", "n": 9},
        {"prompt": "x", "size": "huge"},
        {"prompt": "x", "response_format": "gif"},
    ],
)
def test_validation(plan: Path, auth_header: dict[str, str], bad: dict[str, Any]) -> None:
    client, transport = _client(_images_ok)
    r = client.post("/v1/images/generations", json=bad, headers=auth_header)
    assert r.status_code == 400
    assert not [q for q in transport.requests if "images" in q.url.path]


def test_edits_501(plan: Path, auth_header: dict[str, str]) -> None:
    client, _ = _client(_images_ok)
    r = client.post("/v1/images/edits", json=BODY, headers=auth_header)
    assert r.status_code == 501
    assert client.post("/v1/images/edits", json=BODY).status_code == 401


def test_models_lists_image_model(plan: Path, auth_header: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"slug": "gpt-6-luna"}]})

    client, _ = _client(handler)
    ids = [m["id"] for m in client.get("/v1/models", headers=auth_header).json()["data"]]
    assert "gpt-image-2" in ids


def test_no_login_no_image_model(
    monkeypatch: pytest.MonkeyPatch, auth_header: dict[str, str]
) -> None:
    from nexus.images import image_model_rows

    assert image_model_rows() == []
    client, _ = _client(_images_ok)
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "images_unavailable"


def _nous_ok(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/chat/completions")
    body = json.loads(request.content)
    assert body["model"] == "google/gemini-nano-banana-2.1"
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": "",
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{PNG}"},
                            }
                        ],
                    }
                }
            ]
        },
    )


def test_nous_off_by_default(monkeypatch: pytest.MonkeyPatch, auth_header: dict[str, str]) -> None:
    monkeypatch.setenv("NEXUS_IMAGES_BACKEND", "nous")
    monkeypatch.setenv("NOUS_API_KEY", "nous-test-key")
    client, transport = _client(_nous_ok)
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "nous_images_disabled"
    assert not [q for q in transport.requests if q.url.path.endswith("/chat/completions")]


def test_nous_opt_in_and_cap(monkeypatch: pytest.MonkeyPatch, auth_header: dict[str, str]) -> None:
    monkeypatch.setenv("NEXUS_IMAGES_BACKEND", "nous")
    monkeypatch.setenv("NEXUS_ENABLE_NOUS_IMAGES", "1")
    monkeypatch.setenv("NEXUS_NOUS_IMAGE_DAILY_CAP", "2")
    monkeypatch.setenv("NOUS_API_KEY", "nous-test-key")
    client, transport = _client(_nous_ok)
    for _ in range(2):
        r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
        assert r.status_code == 200, r.text
        assert r.json()["data"][0]["b64_json"] == PNG
        assert r.headers["x-os3-backend"] == "nous"
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "nous_image_cap"
    assert len([q for q in transport.requests if q.url.path.endswith("/chat/completions")]) == 2
    assert "nous-test-key" not in r.text


def test_force_responses_header(plan: Path, auth_header: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/images/generations")
        return httpx.Response(
            200, content=_responses_events(), headers={"content-type": "text/event-stream"}
        )

    client, _ = _client(handler)
    r = client.post(
        "/v1/images/generations",
        json=BODY,
        headers={**auth_header, "X-Nexus-Image-Route": "responses"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-nexus-image-route"] == "responses"


def test_plan_auth_failure(
    plan: Path, auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus import chatgpt_auth

    async def fake_access(force_refresh: bool = False) -> Any:
        return chatgpt_auth.Credentials(
            access_token="tok-secret-xyz", account_id="acct-test-123", expires_at=time.time() + 3600
        )

    monkeypatch.setattr(chatgpt_auth, "access", fake_access)
    client, transport = _client(lambda request: httpx.Response(401, text="tok-secret-xyz bad"))
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "chatgpt_auth_rejected"
    assert "tok-secret-xyz" not in r.text
    assert len([q for q in transport.requests if q.url.path.endswith("/images/generations")]) == 2


def test_timeout_is_504(plan: Path, auth_header: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = _client(handler)
    r = client.post("/v1/images/generations", json=BODY, headers=auth_header)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "chatgpt_timeout"


def test_errors_are_openai_shaped(plan: Path, auth_header: dict[str, str]) -> None:
    client, _ = _client(_images_ok)
    r = client.post("/v1/images/generations", json={"prompt": "x", "n": 0}, headers=auth_header)
    err = r.json()["error"]
    assert r.status_code == 400 and err["type"] == "invalid_request_error" and err["message"]
    r = client.post("/v1/images/generations", content=b"not json", headers=auth_header)
    assert r.status_code == 400
