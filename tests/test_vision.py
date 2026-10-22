"""Image-bearing chat: Responses input_image on chatgpt, and no text-only Nous fallback."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.chatgpt import ChatGPTBackend
from nexus.backends.nous import NousBackend
from nexus.chatgpt_responses import chat_to_responses
from nexus.errors import GatewayError
from nexus.failover import FailoverState

from tests.test_chatgpt import Upstream, _text_events, auth_file  # noqa: F401
from tests.test_chatgpt import _sse as responses_sse
from tests.test_failover import Fake
from tests.transport import HandlerTransport

DATA_URL = "data:image/png;base64,iVBORw0KGgo="
HTTPS_URL = "https://example.com/cat.png"


def _image_messages() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in these?"},
                {"type": "image_url", "image_url": {"url": DATA_URL, "detail": "low"}},
                {"type": "image_url", "image_url": HTTPS_URL},
            ],
        }
    ]


def test_image_url_parts_become_input_image() -> None:
    body = chat_to_responses({"messages": _image_messages()}, model="gpt-6-luna")
    parts = body["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "what is in these?"}
    assert parts[1] == {"type": "input_image", "image_url": DATA_URL, "detail": "low"}
    assert parts[2] == {"type": "input_image", "image_url": HTTPS_URL}


def test_any_os3_model_name_reaches_chatgpt_with_the_image(
    auth_file,  # noqa: F811
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_MODEL_FALLBACK", "on")
    upstream = Upstream([(200, responses_sse(_text_events("a cat")))])
    with TestClient(create_app(backend=ChatGPTBackend(transport=upstream.transport))) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"model": "meituan/longcat-2.5-preview:free", "messages": _image_messages()},
        )
    assert r.status_code == 200, r.text
    sent = upstream.bodies[0]
    assert sent["model"] == "gpt-6-luna"
    kinds = [p["type"] for p in sent["input"][0]["content"]]
    assert kinds == ["input_text", "input_image", "input_image"]


class VisionFake(Fake):
    def __init__(self, backend_id: str, takes: bool | None, **kw: Any) -> None:
        super().__init__(backend_id, **kw)
        self.takes = takes
        self.asked: list[str] = []

    async def accepts_images(self, model: str) -> bool | None:
        self.asked.append(model)
        return self.takes


def _rig(monkeypatch: pytest.MonkeyPatch, bridge_key: str, nous: Fake) -> tuple[TestClient, dict]:
    monkeypatch.setenv("NEXUS_BACKEND", "chatgpt")
    monkeypatch.setenv("NEXUS_FALLBACK", "chatgpt,nous")
    quota = GatewayError(
        429,
        "ChatGPT plan usage limit reached",
        "rate_limit_error",
        "chatgpt_quota",
        extra={
            "upstream": {
                "backend": "chatgpt",
                "upstream_status": 429,
                "upstream_body": '{"error":{"type":"usage_limit_reached"}}',
            }
        },
    )
    fakes = {"chatgpt": Fake("chatgpt", fail=quota), "nous": nous}
    app = create_app(backend=fakes["chatgpt"])
    app.state.failover = FailoverState()
    app.state.backend_factory = fakes.__getitem__
    return TestClient(app, raise_server_exceptions=False), fakes


@pytest.mark.parametrize("takes", [False, None])
def test_text_only_nous_is_skipped_for_images(
    monkeypatch: pytest.MonkeyPatch, bridge_key: str, takes: bool | None
) -> None:
    client, fakes = _rig(monkeypatch, bridge_key, VisionFake("nous", takes))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bridge_key}"},
        json={"model": "x", "messages": _image_messages()},
    )
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "chatgpt_quota"
    assert err["fallback"]["skipped"] == ["nous"]
    assert "does not take images" in err["hint"]
    assert fakes["nous"].calls == []


def test_vision_nous_takes_the_image_fallback(
    monkeypatch: pytest.MonkeyPatch, bridge_key: str
) -> None:
    client, fakes = _rig(monkeypatch, bridge_key, VisionFake("nous", True))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bridge_key}"},
        json={"model": "x", "messages": _image_messages()},
    )
    assert r.status_code == 200, r.text
    assert "from nous" in r.text
    assert len(fakes["nous"].calls) == 1


def test_text_requests_do_not_ask_about_images(
    monkeypatch: pytest.MonkeyPatch, bridge_key: str
) -> None:
    nous = VisionFake("nous", False)
    client, _ = _rig(monkeypatch, bridge_key, nous)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bridge_key}"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert nous.asked == []


def _models_handler(rows: list[dict[str, Any]]):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": rows})

    return handle


def test_nous_reads_input_modalities(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_modalities(monkeypatch))


async def _modalities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOUS_API_KEY", "nous-test-key")
    rows = [
        {"id": "v", "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "t", "architecture": {"input_modalities": ["text"]}},
        {"id": "m", "architecture": {"modality": "text+image->text"}},
        {"id": "u"},
    ]
    nous = NousBackend(transport=HandlerTransport(_models_handler(rows)))
    assert await nous.accepts_images("v") is True
    assert await nous.accepts_images("t") is False
    assert await nous.accepts_images("m") is True
    assert await nous.accepts_images("u") is None
    assert await nous.accepts_images("absent") is False
    assert json.dumps(rows)
