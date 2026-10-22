"""POST /v1/messages translation. No live OpenRouter."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient
from nexus.anthropic_messages import (
    AnthropicSseTranslator,
    to_anthropic_message,
    to_openai_chat,
)
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL

from tests.transport import HandlerTransport

COMPLETION = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "created": 1,
    "model": DEFAULT_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "OS3_BRIDGE_OK",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "ping", "arguments": '{"x":1}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
}


def _client(transport: HandlerTransport | None = None) -> TestClient:
    backend = OpenRouterBackend(transport=transport) if transport is not None else None
    return TestClient(create_app(backend=backend))


def _events(text: str) -> list[tuple[str | None, dict]]:
    found: list[tuple[str | None, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = None
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].lstrip())
        found.append((name, json.loads("\n".join(data))))
    return found


def test_request_translation_tools_images_and_model_map() -> None:
    payload = to_openai_chat(
        {
            "model": "claude-3-5-sonnet-latest",
            "max_tokens": 32,
            "temperature": 0,
            "stop_sequences": ["END"],
            "system": [
                {"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}
            ],
            "tool_choice": {"type": "any"},
            "tools": [
                {
                    "name": "ping",
                    "description": "ping",
                    "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abcd",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "secret scratchpad"},
                        {"type": "text", "text": "calling"},
                        {"type": "tool_use", "id": "toolu_1", "name": "ping", "input": {"x": 1}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "pong"}
                    ],
                },
            ],
        }
    )
    assert payload["model"] == "anthropic/claude-3-5-sonnet-latest"
    assert payload["max_tokens"] == 32
    assert payload["temperature"] == 0.0
    assert payload["stop"] == ["END"]
    assert payload["tool_choice"] == "required"
    assert payload["stream"] is False
    assert "stream_options" not in payload
    assert payload["tools"][0]["function"]["parameters"]["properties"]["x"]["type"] == "integer"
    assert payload["messages"][0] == {"role": "system", "content": "be brief"}
    assert payload["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,abcd"
    assistant = payload["messages"][2]
    assert assistant["content"] == "calling"
    assert "secret scratchpad" not in json.dumps(assistant)
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
    assert payload["messages"][3]["role"] == "tool"
    assert payload["messages"][3]["name"] == "ping"
    assert payload["messages"][3]["content"] == "pong"
    slash = to_openai_chat(
        {
            "model": DEFAULT_MODEL,
            "max_tokens": 8,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert slash["model"] == DEFAULT_MODEL
    assert slash["stream_options"] == {"include_usage": True}
    defaulted = to_openai_chat({"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]})
    assert defaulted["model"] == DEFAULT_MODEL


def test_response_translation_maps_tool_use_and_stop_reason() -> None:
    message = to_anthropic_message(COMPLETION, fallback_model="fallback")
    assert message["id"] == "msg_abc"
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert message["content"][0] == {"type": "text", "text": "OS3_BRIDGE_OK"}
    assert message["content"][1]["type"] == "tool_use"
    assert message["content"][1]["input"] == {"x": 1}
    length = {
        "id": "chatcmpl-x",
        "model": "m",
        "choices": [
            {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "cut"},
            }
        ],
    }
    assert to_anthropic_message(length, fallback_model="m")["stop_reason"] == "max_tokens"


def test_stream_translator_reassembles_split_chunks() -> None:
    translator = AnthropicSseTranslator(DEFAULT_MODEL)
    raw = b'data: {"id":"chatcmpl-1","model":"m","choices":[{"delta":{"content":"abc"}}]}\n\n'
    first = translator.feed(raw[:40])
    assert first == []
    second = translator.feed(raw[40:])
    done = translator.finish()
    events = _events((b"".join(first + second + done)).decode())
    names = [name for name, _payload in events]
    assert names[0] == "message_start"
    assert events[0][1]["message"]["id"] == "msg_1"
    deltas = [
        payload["delta"]["text"]
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "text_delta"
    ]
    assert deltas == ["abc"]
    assert names[-1] == "message_stop"
    assert translator.finish() == []


def test_messages_requires_bearer_in_anthropic_shape(bridge_key: str) -> None:
    with _client() as client:
        missing = client.post(
            "/v1/messages",
            json={"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert missing.status_code == 401
    body = missing.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["message"] == "Missing Bearer token"
    assert "code" not in body["error"]
    assert bridge_key not in missing.text
    assert missing.headers["www-authenticate"].lower().startswith("bearer")


def test_messages_nonstream_proxies_openrouter(
    auth_header: dict[str, str],
    bridge_key: str,
    openrouter_key: str,
) -> None:
    transport = HandlerTransport(lambda _request: httpx.Response(200, json=COMPLETION))
    request = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 16,
        "system": "be brief",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "ping",
                "description": "ping",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": {"type": "tool", "name": "ping"},
    }
    with _client(transport) as client:
        response = client.post("/v1/messages", headers=auth_header, json=request)
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "OS3_BRIDGE_OK"
    assert body["content"][1]["name"] == "ping"
    assert body["stop_reason"] == "tool_use"
    sent = json.loads(transport.requests[0].content.decode())
    assert sent["model"] == "anthropic/claude-sonnet-4-5"
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["messages"][1] == {"role": "user", "content": "hi"}
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "ping"}}
    assert sent["stream"] is False
    assert transport.requests[0].headers["authorization"] == f"Bearer {openrouter_key}"
    assert bridge_key not in response.text
    assert openrouter_key not in response.text
    assert str(transport.requests[0].url).endswith("/chat/completions")


def test_messages_stream_translates_tool_calls(
    auth_header: dict[str, str],
    openrouter_key: str,
) -> None:
    frames = [
        {
            "id": "chatcmpl-9",
            "model": DEFAULT_MODEL,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}],
        },
        {"choices": [{"index": 0, "delta": {"content": "lo " + openrouter_key}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_9",
                                "type": "function",
                                "function": {"name": "Bash", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"command":"ls"}'}}]
                    },
                }
            ]
        },
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        },
    ]
    sse = (
        b"".join(
            f"data: {json.dumps(frame, separators=(',', ':'))}\n\n".encode() for frame in frames
        )
        + b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    transport = HandlerTransport(handler)
    with _client(transport) as client:
        response = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": DEFAULT_MODEL,
                "max_tokens": 16,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert openrouter_key not in response.text
    events = _events(response.text)
    names = [name for name, _payload in events]
    assert names[0] == "message_start"
    assert "content_block_start" in names
    text = "".join(
        payload["delta"]["text"]
        for _name, payload in events
        if payload.get("type") == "content_block_delta" and payload["delta"]["type"] == "text_delta"
    )
    assert text.startswith("Hello ")
    assert "[REDACTED]" in text
    tool_starts = [
        payload["content_block"]
        for _name, payload in events
        if payload.get("type") == "content_block_start"
        and payload["content_block"]["type"] == "tool_use"
    ]
    assert tool_starts[0]["name"] == "Bash"
    assert tool_starts[0]["id"] == "call_9"
    partial = "".join(
        payload["delta"]["partial_json"]
        for _name, payload in events
        if payload.get("type") == "content_block_delta"
        and payload["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(partial) == {"command": "ls"}
    delta = next(payload for name, payload in events if name == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["output_tokens"] == 4
    assert delta["usage"]["input_tokens"] == 3
    assert names[-1] == "message_stop"


def test_messages_errors_are_anthropic_shaped(
    auth_header: dict[str, str],
    openrouter_key: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        text = body["messages"][-1]["content"]
        if text == "limit":
            return httpx.Response(
                429,
                json={"error": {"message": f"slow down {openrouter_key}", "code": "rate"}},
            )
        return httpx.Response(401, json={"error": {"message": "bad upstream key"}})

    transport = HandlerTransport(handler)
    with _client(transport) as client:
        invalid = client.post("/v1/messages", headers=auth_header, content=b"{")
        missing = client.post(
            "/v1/messages",
            headers=auth_header,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        limited = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "limit"}],
            },
        )
        upstream_auth = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "auth"}],
            },
        )
    assert invalid.status_code == 400
    assert invalid.json()["type"] == "error"
    assert invalid.json()["error"]["type"] == "invalid_request_error"
    assert "code" not in invalid.json()["error"]
    assert missing.status_code == 400
    assert "max_tokens" in missing.json()["error"]["message"]
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limit_error"
    assert limited.json()["error"]["message"] == "slow down [REDACTED]"
    assert openrouter_key not in limited.text
    assert upstream_auth.status_code == 502
    assert upstream_auth.json()["error"]["type"] == "api_error"
    assert "OpenRouter rejected the upstream credential" in upstream_auth.json()["error"]["message"]


def test_messages_missing_openrouter_key_stays_anthropic(
    auth_header: dict[str, str],
    bridge_key: str,
) -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("messages must not call upstream without a key")

    transport = HandlerTransport(explode)
    with _client(transport) as client:
        response = client.post(
            "/v1/messages",
            headers=auth_header,
            json={"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "api_error"
    assert "OpenRouter" in response.json()["error"]["message"]
    assert bridge_key not in response.text
    assert "sk-" not in response.text
    assert transport.requests == []
