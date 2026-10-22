"""Parallel tool calls and screenshot parts. Mocked upstreams only."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.anthropic_chat import OpenAISseFromAnthropic, openai_chat_to_anthropic
from nexus.anthropic_messages import AnthropicSseTranslator, to_anthropic_message
from nexus.app import create_app
from nexus.backends.anthropic import AnthropicBackend
from nexus.backends.openai import OpenAIBackend
from nexus.backends.openrouter import OpenRouterBackend
from nexus.backends.xai import XAIBackend

from tests.transport import HandlerTransport

PNG = base64.b64encode(b"png-bytes").decode()
DATA_PNG = f"data:image/png;base64,{PNG}"

CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "click", "arguments": '{"x":1}'},
    },
    {
        "id": "call_2",
        "type": "function",
        "function": {"name": "type_text", "arguments": '{"text":"hi"}'},
    },
]

TOOLS = [
    {"type": "function", "function": {"name": "click", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "type_text", "parameters": {"type": "object"}}},
]

CHAT = {
    "model": "vision-model",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "screen"},
                {"type": "image_url", "image_url": {"url": DATA_PNG}},
            ],
        },
        {"role": "assistant", "content": None, "tool_calls": CALLS},
        {"role": "tool", "tool_call_id": "call_1", "content": "clicked"},
        {"role": "tool", "tool_call_id": "call_2", "content": "typed"},
    ],
    "tools": TOOLS,
    "parallel_tool_calls": True,
}

MULTI = {
    "id": "chatcmpl-multi",
    "object": "chat.completion",
    "created": 1,
    "model": "vision-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": CALLS},
            "finish_reason": "tool_calls",
        }
    ],
}


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


@pytest.mark.parametrize(
    ("factory", "url", "env_name", "secret"),
    [
        (
            OpenRouterBackend,
            "https://openrouter.ai/api/v1/chat/completions",
            "OPENROUTER_API_KEY",
            "sk-or-fidelity-secret",
        ),
        (
            OpenAIBackend,
            "https://api.openai.com/v1/chat/completions",
            "OPENAI_API_KEY",
            "sk-proj-fidelity-secret",
        ),
        (XAIBackend, "https://api.x.ai/v1/chat/completions", "XAI_API_KEY", "xai-fidelity-secret"),
    ],
)
def test_openai_compatible_forwards_parallel_tools_and_images(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    factory: type,
    url: str,
    env_name: str,
    secret: str,
) -> None:
    monkeypatch.setenv(env_name, secret)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("stream"):
            frame = {
                "id": "chatcmpl-multi",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "click", "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "call_2",
                                    "function": {"name": "type_text", "arguments": "{}"},
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(frame) + b"data: [DONE]\n\n",
            )
        return httpx.Response(200, json=MULTI)

    transport = HandlerTransport(handler)
    with TestClient(create_app(backend=factory(transport=transport))) as client:
        chat = client.post("/v1/chat/completions", headers=auth_header, json=CHAT)
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={**CHAT, "stream": True},
        )
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["tool_calls"] == CALLS
    assert streamed.status_code == 200
    assert "click" in streamed.text and "type_text" in streamed.text
    sent = [json.loads(request.content.decode()) for request in transport.requests]
    assert sent[0] == {**CHAT, "stream": False}
    assert sent[1]["stream"] is True
    assert sent[1]["tools"] == TOOLS
    assert sent[1]["messages"][0]["content"][1]["image_url"]["url"] == DATA_PNG
    assert sent[1]["messages"][1]["tool_calls"] == CALLS
    assert [str(request.url) for request in transport.requests] == [url, url]
    assert secret not in chat.text
    assert secret not in streamed.text


def test_anthropic_chat_round_trips_parallel_tools_and_images(
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fidelity-secret")
    message = {
        "id": "msg_multi",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "call_1", "name": "click", "input": {"x": 1}},
            {"type": "tool_use", "id": "call_2", "name": "type_text", "input": {"text": "hi"}},
        ],
        "usage": {"input_tokens": 4, "output_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("stream"):
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_multi",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 4, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "click",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "type_text",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"text":"hi"}'},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 3},
                },
                {"type": "message_stop"},
            ]
            raw = b"".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                for event in events
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=raw,
            )
        return httpx.Response(200, json=message)

    transport = HandlerTransport(handler)
    with TestClient(create_app(backend=AnthropicBackend(transport=transport))) as client:
        chat = client.post("/v1/chat/completions", headers=auth_header, json=CHAT)
        streamed = client.post(
            "/v1/chat/completions",
            headers=auth_header,
            json={**CHAT, "stream": True},
        )
        native = client.post(
            "/v1/messages",
            headers=auth_header,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 32,
                "tools": [
                    {"name": "click", "input_schema": {"type": "object"}},
                    {"name": "type_text", "input_schema": {"type": "object"}},
                ],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "screen"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": PNG,
                                },
                            },
                        ],
                    }
                ],
            },
        )
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert chat.json()["choices"][0]["message"]["tool_calls"] == CALLS
    names = []
    for line in streamed.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line.removeprefix("data: "))
        delta = payload["choices"][0]["delta"]
        for call in delta.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name"):
                names.append(function["name"])
    assert names == ["click", "type_text"]
    sent = json.loads(transport.requests[0].content.decode())
    assert [tool["name"] for tool in sent["tools"]] == ["click", "type_text"]
    assert sent["messages"][0]["content"][1]["source"]["data"] == PNG
    assistant = sent["messages"][1]["content"]
    assert [block["name"] for block in assistant] == ["click", "type_text"]
    results = sent["messages"][2]["content"]
    assert [block["tool_use_id"] for block in results] == ["call_1", "call_2"]
    assert results[0]["content"] == "clicked"
    assert results[1]["content"] == "typed"
    native_body = json.loads(transport.requests[2].content.decode())
    assert native_body["messages"][0]["content"][1]["source"]["data"] == PNG
    assert [tool["name"] for tool in native_body["tools"]] == ["click", "type_text"]
    assert native.status_code == 200
    assert native.json()["content"][0]["name"] == "click"
    assert native.json()["content"][1]["name"] == "type_text"
    assert "sk-ant-fidelity-secret" not in chat.text


def test_legacy_function_call_and_input_image_map_to_anthropic() -> None:
    converted = openai_chat_to_anthropic(
        {
            "model": "claude-sonnet-4-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": {"url": DATA_PNG}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "function_call": {"name": "click", "arguments": '{"x":1}'},
                },
            ],
            "functions": [
                {"name": "click", "parameters": {"type": "object", "properties": {}}},
            ],
        }
    )
    assert converted["tools"][0]["name"] == "click"
    assert converted["messages"][0]["content"][0]["source"]["data"] == PNG
    assert converted["messages"][1]["content"][0]["type"] == "tool_use"
    assert converted["messages"][1]["content"][0]["name"] == "click"
    message = to_anthropic_message(
        {
            "id": "chatcmpl-fn",
            "model": "m",
            "choices": [
                {
                    "finish_reason": "function_call",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "function_call": {"name": "click", "arguments": '{"x":1}'},
                    },
                }
            ],
        },
        fallback_model="m",
    )
    assert message["stop_reason"] == "tool_use"
    assert message["content"][0]["name"] == "click"
    assert message["content"][0]["input"] == {"x": 1}


def test_messages_stream_keeps_two_tool_calls() -> None:
    translator = AnthropicSseTranslator("vision-model")
    frames = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "click", "arguments": ""},
                            },
                            {
                                "index": 1,
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "type_text", "arguments": ""},
                            },
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"x":1}'}},
                            {"index": 1, "function": {"arguments": '{"text":"hi"}'}},
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    raw = b"".join(_sse(frame) for frame in frames)
    events = translator.feed(raw) + translator.finish()
    text = b"".join(events).decode()
    assert text.count('"type":"tool_use"') == 2
    assert '"name":"click"' in text
    assert '"name":"type_text"' in text
    partials: list[str] = []
    for block in text.split("\n\n"):
        data = ""
        for line in block.splitlines():
            if line.startswith("data:"):
                data = line.removeprefix("data:").strip()
        if not data:
            continue
        payload = json.loads(data)
        delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        if delta.get("type") == "input_json_delta":
            partials.append(delta["partial_json"])
    assert partials == ['{"x":1}', '{"text":"hi"}']


def test_anthropic_sse_emits_parallel_openai_tool_calls() -> None:
    translator = OpenAISseFromAnthropic("claude-sonnet-4-5")
    events = [
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "call_1", "name": "click", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'},
        },
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "call_2",
                "name": "type_text",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"text":"hi"}'},
        },
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
        {"type": "message_stop"},
    ]
    raw = b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )
    chunks = translator.feed(raw) + translator.finish()
    names: list[str] = []
    arguments: list[str] = []
    for chunk in chunks:
        line = chunk.decode().strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line.removeprefix("data: "))
        for call in payload["choices"][0]["delta"].get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name"):
                names.append(function["name"])
            if function.get("arguments"):
                arguments.append(function["arguments"])
    assert names == ["click", "type_text"]
    assert arguments == ['{"x":1}', '{"text":"hi"}']
