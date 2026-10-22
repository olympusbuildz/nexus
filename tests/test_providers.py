"""OpenAI, Anthropic, and xAI backends. Mocked httpx only — no live network."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.anthropic_chat import openai_chat_to_anthropic
from nexus.app import create_app
from nexus.backends.anthropic import AnthropicBackend
from nexus.backends.openai import OpenAIBackend
from nexus.backends.xai import XAIBackend

from tests.transport import HandlerTransport, sent_to

OPENAI_COMPLETION = {
    "id": "chatcmpl-openai",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "OS3_BRIDGE_OK"},
            "finish_reason": "stop",
        }
    ],
}

ANTHROPIC_MESSAGE = {
    "id": "msg_abc",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "OS3_BRIDGE_OK"}],
    "model": "claude-sonnet-4-5",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 2},
}


def _sse(text: str) -> bytes:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-5",
                "stop_reason": None,
                "usage": {"input_tokens": 3, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def test_openai_chat_models_and_env_over_file(
    auth_header: dict[str, str],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_key = "sk-proj-file-secret-value"
    env_key = "sk-proj-env-secret-value"
    path = tmp_path / "openai.key"
    path.write_text(file_key + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openai")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-4o", "object": "model", "created": 4, "owned_by": "openai"}
                    ]
                },
            )
        return httpx.Response(200, json=OPENAI_COMPLETION)

    transport = HandlerTransport(handler)
    app = create_app(backend=OpenAIBackend(transport=transport))
    with TestClient(app) as client:
        models = client.get("/v1/models", headers=auth_header)
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        monkeypatch.setenv("OPENAI_API_KEY", env_key)
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "gpt-4o-mini"
    assert models.json()["data"][1]["id"] == "gpt-4o"
    assert str(transport.requests[0].url) == "https://api.openai.com/v1/models"
    assert transport.requests[0].headers["authorization"] == f"Bearer {file_key}"
    assert "http-referer" not in transport.requests[0].headers
    assert chat.status_code == 200
    sent = json.loads(transport.requests[1].content.decode())
    assert sent["model"] == "gpt-4o-mini"
    assert str(transport.requests[1].url) == "https://api.openai.com/v1/chat/completions"
    assert transport.requests[1].headers["authorization"] == f"Bearer {file_key}"
    assert file_key not in chat.text
    assert streamed.status_code == 200
    assert transport.requests[2].headers["authorization"] == f"Bearer {env_key}"
    assert file_key not in streamed.text
    assert env_key not in streamed.text


def test_openai_stream_proxies_sse(
    auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-stream-secret")
    sse = b'data: {"choices":[{"delta":{"content":"OS3_BRIDGE_OK"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content.decode())["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    transport = HandlerTransport(handler)
    with TestClient(create_app(backend=OpenAIBackend(transport=transport))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "OS3_BRIDGE_OK" in response.text
    assert "data: [DONE]" in response.text
    assert "sk-proj-stream-secret" not in response.text


def test_messages_translate_on_openai_does_not_prefix_claude(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-messages-secret")
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=OPENAI_COMPLETION))
    with TestClient(create_app(backend=OpenAIBackend(transport=transport))) as client:
        response = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["type"] == "message"
    assert response.json()["content"][0]["text"] == "OS3_BRIDGE_OK"
    sent = json.loads(transport.requests[0].content.decode())
    assert sent["model"] == "claude-sonnet-4-5"
    assert str(transport.requests[0].url) == "https://api.openai.com/v1/chat/completions"
    assert "sk-proj-messages-secret" not in response.text


def test_xai_chat_and_models(auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "xai")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-secret-value")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "grok-3", "object": "model", "created": 2, "owned_by": "xai"}]
                },
            )
        body = json.loads(request.content.decode())
        if body.get("stream"):
            sse = b'data: {"choices":[{"delta":{"content":"OS3_BRIDGE_OK"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
        completion = dict(OPENAI_COMPLETION)
        completion["model"] = "grok-4"
        return httpx.Response(200, json=completion)

    transport = HandlerTransport(handler)
    with TestClient(create_app(backend=XAIBackend(transport=transport))) as client:
        health = client.get("/health").json()
        models = client.get("/v1/models", headers=auth_header)
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "model": "grok-4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert health["backend"] == "xai"
    assert health["backends"]["xai"]["implemented"] is True
    assert health["backends"]["xai"]["ready"] is True
    assert health["backends"]["xai"]["key_present"] is True
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "grok-4"
    assert any(str(request.url) == "https://api.x.ai/v1/models" for request in transport.requests)
    assert chat.status_code == 200
    chats = sent_to(transport, "/v1/chat/completions")
    sent = json.loads(chats[0].content.decode())
    assert sent["model"] == "grok-4"
    assert chats[0].headers["authorization"] == "Bearer xai-test-secret-value"
    assert streamed.status_code == 200
    assert "OS3_BRIDGE_OK" in streamed.text
    assert "xai-test-secret-value" not in chat.text
    assert "xai-test-secret-value" not in streamed.text


def test_messages_translate_on_xai(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "xai")
    monkeypatch.setenv("XAI_API_KEY", "xai-messages-secret")
    completion = dict(OPENAI_COMPLETION)
    completion["model"] = "grok-4"
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=completion))
    with TestClient(create_app(backend=XAIBackend(transport=transport))) as client:
        response = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "grok-4",
                "max_tokens": 8,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: message_start" in response.text
    assert "OS3_BRIDGE_OK" in response.text
    sent = json.loads(transport.requests[0].content.decode())
    assert sent["model"] == "grok-4"
    assert sent["stream"] is True
    assert str(transport.requests[0].url) == "https://api.x.ai/v1/chat/completions"
    assert "xai-messages-secret" not in response.text


def test_anthropic_native_messages_and_translated_chat(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")
    key = "sk-ant-test-secret-value"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == key
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        body = json.loads(request.content.decode())
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse("OS3_BRIDGE_OK " + key),
            )
        return httpx.Response(200, json=ANTHROPIC_MESSAGE)

    transport = HandlerTransport(handler)
    with TestClient(create_app(backend=AnthropicBackend(transport=transport))) as client:
        health = client.get("/health").json()
        messages = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "anthropic/claude-sonnet-4-5",
                "max_tokens": 16,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        streamed = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 16,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        chat = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "messages": [
                    {"role": "system", "content": "brief"},
                    {"role": "user", "content": "hi"},
                ]
            },
        )
        chat_stream = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert health["backends"]["anthropic"]["implemented"] is True
    assert health["backends"]["anthropic"]["ready"] is True
    assert health["routes"]["/v1/messages"]["mode"] == "native"
    assert health["routes"]["/v1/messages"]["backend"] == "anthropic"
    native = json.loads(sent_to(transport, "/v1/messages")[0].content.decode())
    assert native["model"] == "claude-sonnet-4-5"
    assert native["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "stream_options" not in native
    assert messages.status_code == 200
    assert messages.json()["type"] == "message"
    assert messages.json()["content"][0]["text"] == "OS3_BRIDGE_OK"
    assert streamed.status_code == 200
    assert "event: message_start" in streamed.text
    assert "OS3_BRIDGE_OK" in streamed.text
    assert "chat.completion.chunk" not in streamed.text
    assert key not in streamed.text
    chat_body = next(
        json.loads(request.content.decode())
        for request in sent_to(transport, "/v1/messages")
        if "system" in json.loads(request.content.decode())
    )
    assert chat_body["model"] == "claude-sonnet-4-5"
    assert chat_body["system"] == "brief"
    assert chat_body["max_tokens"] == 4096
    assert chat.status_code == 200
    assert chat.json()["object"] == "chat.completion"
    assert chat.json()["choices"][0]["message"]["content"] == "OS3_BRIDGE_OK"
    assert chat.json()["choices"][0]["finish_reason"] == "stop"
    assert chat_stream.status_code == 200
    assert "chat.completion.chunk" in chat_stream.text
    assert "OS3_BRIDGE_OK" in chat_stream.text
    assert "data: [DONE]" in chat_stream.text
    assert key not in messages.text
    assert key not in chat.text
    assert key not in chat_stream.text


def test_anthropic_models_list(
    auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-models-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "model",
                        "id": "claude-haiku-4-5",
                        "display_name": "Haiku",
                        "created_at": "2025-10-01T00:00:00Z",
                    }
                ]
            },
        )

    transport = HandlerTransport(handler)
    with TestClient(create_app(backend=AnthropicBackend(transport=transport))) as client:
        response = client.get("/v1/models", headers=auth_header)
    assert response.status_code == 200
    rows = response.json()["data"]
    assert rows[0]["id"] == "claude-sonnet-4-5"
    assert rows[0]["owned_by"] == "nexus"
    assert rows[1]["id"] == "claude-haiku-4-5"
    assert rows[1]["owned_by"] == "anthropic"
    assert isinstance(rows[1]["created"], int)
    assert "sk-ant-models-secret" not in response.text


def test_missing_provider_key_does_not_call_upstream(auth_header: dict[str, str]) -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing key must not call upstream")

    transport = HandlerTransport(explode)
    with TestClient(create_app(backend=OpenAIBackend(transport=transport))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "openai_key_missing"
    assert transport.requests == []


def test_chat_to_anthropic_maps_tools_and_strips_vendor_prefix() -> None:
    converted = openai_chat_to_anthropic(
        {
            "model": "anthropic/claude-sonnet-4-5",
            "max_tokens": 32,
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "ping", "arguments": '{"x":1}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "pong"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "required",
        }
    )
    assert converted["model"] == "claude-sonnet-4-5"
    assert converted["system"] == "be brief"
    assert converted["tool_choice"] == {"type": "any"}
    assert converted["tools"][0]["name"] == "ping"
    assistant = converted["messages"][1]
    assert assistant["content"][0]["type"] == "tool_use"
    assert assistant["content"][0]["input"] == {"x": 1}
    assert converted["messages"][2]["content"][0]["type"] == "tool_result"
    assert converted["messages"][2]["content"][0]["content"] == "pong"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_not_reported_as_a_bad_model(
    status: int, bridge_key: str, openrouter_key: str
) -> None:
    """The first-run failure is a mistyped key; the error must say so."""
    from nexus.backends.openrouter import OpenRouterBackend

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "no"}})

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    error = response.json()["error"]
    assert "API key" in error["message"]
    assert "rejected model" not in error["message"]
    assert "./nexus setup --backend openrouter" in error["hint"]
    assert error["upstream"]["upstream_status"] == status
    assert openrouter_key not in response.text


def test_a_rate_limit_is_not_reported_as_a_bad_model(bridge_key: str, openrouter_key: str) -> None:
    from nexus.backends.openrouter import OpenRouterBackend

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"retry-after": "30"}, json={"error": {"message": "slow down"}}
        )

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    error = response.json()["error"]
    assert "rate limiting" in error["message"]
    assert "rejected model" not in error["message"]
    assert "NEXUS_MODEL" not in error["hint"]
    assert "30s" in error["hint"]
    assert error["upstream"]["upstream_status"] == 429
    assert openrouter_key not in response.text


def test_a_missing_model_still_points_at_the_model_id(bridge_key: str, openrouter_key: str) -> None:
    from nexus.backends.openrouter import OpenRouterBackend

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such model"}})

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"model": "nope/nope", "messages": [{"role": "user", "content": "hi"}]},
        )
    error = response.json()["error"]
    assert "rejected model 'nope/nope'" in error["message"]
    assert "NEXUS_MODEL" in error["hint"]
