"""Streams always end cleanly and every error body carries a hint. Canned upstreams only."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import _terminated, create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL

from tests.transport import HandlerTransport

DONE = b"data: [DONE]\n\n"
TEXT = b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
# Nous ends the stream with a flagged chunk and never sends [DONE].
NOUS_LAST = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"lastOne":true}\n\n'


def _client(sse: bytes) -> TestClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    backend = OpenRouterBackend(transport=HandlerTransport(handler))
    return TestClient(create_app(backend=backend))


def _chat(client: TestClient, headers: dict[str, str], path: str = "/v1/chat/completions"):
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    if path == "/v1/messages":
        body["max_tokens"] = 16
    return client.post(path, headers=headers, json=body)


def test_chat_stream_gets_done_when_upstream_omits_it(
    auth_header: dict[str, str], openrouter_key: str
) -> None:
    with _client(TEXT + NOUS_LAST) as client:
        response = _chat(client, auth_header)
    assert response.status_code == 200
    assert response.content == TEXT + NOUS_LAST + DONE


def test_chat_stream_done_is_not_duplicated(
    auth_header: dict[str, str], openrouter_key: str
) -> None:
    with _client(TEXT + DONE) as client:
        response = _chat(client, auth_header)
    assert response.content == TEXT + DONE
    assert response.text.count("[DONE]") == 1


def test_messages_stream_closes_without_done(
    auth_header: dict[str, str], openrouter_key: str
) -> None:
    with _client(TEXT + NOUS_LAST) as client:
        response = _chat(client, auth_header, "/v1/messages")
    assert response.status_code == 200
    assert response.text.count("event: message_stop") == 1


class _CutStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield TEXT
        raise ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac")


def test_chat_stream_tls_error_mid_stream_ends_cleanly(
    auth_header: dict[str, str], openrouter_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=headers, stream=_CutStream())

    backend = OpenRouterBackend(transport=HandlerTransport(handler))
    with TestClient(create_app(backend=backend), raise_server_exceptions=False) as client:
        response = _chat(client, auth_header)
    assert response.status_code == 200
    assert response.content.startswith(TEXT)
    assert '"code": "upstream_ssl_error"' in response.text
    assert response.content.endswith(DONE)
    assert response.text.count("[DONE]") == 1
    assert "upstream SSL error mid-stream backend=openrouter" in caplog.text


async def _run(*chunks: bytes) -> bytes:
    async def upstream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return b"".join([c async for c in _terminated(upstream())])


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((TEXT,), TEXT + DONE),
        ((TEXT, DONE), TEXT + DONE),
        ((TEXT, b"data: [DO", b"NE]\n\n"), TEXT + b"data: [DO" + b"NE]\n\n"),
        ((TEXT, b"data: [DONE]"), TEXT + b"data: [DONE]"),
        ((b'data: {"a":1}\n',), b'data: {"a":1}\n' + b"\n" + DONE),
        ((b'data: {"a":1}',), b'data: {"a":1}' + b"\n\n" + DONE),
        ((), DONE),
    ],
)
def test_terminated_chunk_shapes(chunks: tuple[bytes, ...], expected: bytes) -> None:
    assert asyncio.run(_run(*chunks)) == expected


def test_terminated_adds_nothing_when_the_stream_breaks() -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield TEXT
        raise RuntimeError("upstream dropped")

    async def collect() -> list[bytes]:
        out: list[bytes] = []
        with pytest.raises(RuntimeError):
            async for chunk in _terminated(broken()):
                out.append(chunk)
        return out

    assert asyncio.run(collect()) == [TEXT]


def test_auth_and_body_errors_carry_hint(bridge_key: str, openrouter_key: str) -> None:
    transport = HandlerTransport(lambda _r: httpx.Response(500))
    app = create_app(backend=OpenRouterBackend(transport=transport))
    good = {"Authorization": f"Bearer {bridge_key}"}
    with TestClient(app) as client:
        responses = [
            client.get("/v1/models"),
            client.get("/v1/models", headers={"Authorization": "Bearer wrong"}),
            client.post("/v1/chat/completions", headers=good, content=b"{"),
            client.post("/v1/chat/completions", headers=good, json=[]),
            client.post("/v1/chat/completions", headers=good, json={}),
            client.post(
                "/v1/chat/completions",
                headers=good,
                json={"messages": [{"role": "user", "content": "x"}], "stream": "yes"},
            ),
            client.get("/v1/nope", headers=good),
        ]
    assert [r.status_code for r in responses] == [401, 401, 400, 400, 400, 400, 404]
    for response in responses:
        hint = response.json()["error"]["hint"]
        assert isinstance(hint, str) and hint.strip()
        assert bridge_key not in response.text


def test_upstream_error_keeps_its_own_hint(bridge_key: str, openrouter_key: str) -> None:
    transport = HandlerTransport(lambda _r: httpx.Response(404, json={"error": "no model"}))
    app = create_app(backend=OpenRouterBackend(transport=transport))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "x"}]},
        )
    assert response.status_code >= 400
    assert response.json()["error"]["hint"].strip()


def test_messages_errors_carry_hint(bridge_key: str, openrouter_key: str) -> None:
    with TestClient(create_app()) as client:
        missing = client.post("/v1/messages", json={})
        bad_json = client.post(
            "/v1/messages", headers={"Authorization": f"Bearer {bridge_key}"}, content=b"{"
        )
    for response in (missing, bad_json):
        assert json.loads(response.text)["error"]["hint"].strip()
