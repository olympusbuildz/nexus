"""Anthropic Messages ↔ OpenAI chat completions.

POST /v1/messages uses this module for OpenRouter, OpenAI, xAI, and the
subscription CLIs. The native Anthropic backend forwards
Messages instead of calling to_openai_chat.

MVP gaps on the translated path, on purpose: prompt-cache controls and extended
thinking are dropped. Tool use, text, images, and streaming are translated for
API backends. Subscription CLIs then accept that text, attach inline
screenshots, and still reject client tool calls.
"""

from __future__ import annotations

import json
from typing import Any

from nexus.config import active_backend_id, default_model, model_for
from nexus.errors import GatewayError

_IMPLEMENTED_MESSAGES = {
    "openrouter",
    "openai",
    "anthropic",
    "xai",
    "nous",
    "chatgpt",
    "codex",
    "claude-code",
    "copilot",
    "grok-build",
    "antigravity",
}
_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "xai": "xAI",
    "nous": "Nous Portal",
    "chatgpt": "ChatGPT plan",
    "codex": "Codex",
    "claude-code": "Claude Code",
    "copilot": "GitHub Copilot",
    "grok-build": "Grok Build",
    "antigravity": "Antigravity",
}

_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def messages_route_status(backend: str) -> dict[str, Any]:
    if backend == "anthropic":
        mode = "native"
    elif backend in _IMPLEMENTED_MESSAGES and backend != "anthropic":
        mode = "translate"
    else:
        mode = "unavailable"
    return {
        "implemented": backend in _IMPLEMENTED_MESSAGES,
        "backend": backend,
        "mode": mode,
    }


def prepare_native_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a Messages body and forward it to the Anthropic API.

    Unknown fields (thinking, cache_control, metadata) are kept.
    """
    messages = payload.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or any(not isinstance(item, dict) for item in messages)
    ):
        raise GatewayError(
            400,
            "messages must be a non-empty list of objects",
            "invalid_request_error",
            "invalid_messages",
        )
    out = dict(payload)
    out["model"] = _native_model(payload.get("model"))
    out["max_tokens"] = _positive_int(payload.get("max_tokens"), "max_tokens")
    out["stream"] = _stream(payload)
    return out


def to_openai_chat(payload: dict[str, Any], *, prefix_claude: bool | None = None) -> dict[str, Any]:
    """Validate one Messages request and return an OpenAI chat payload."""
    if prefix_claude is None:
        prefix_claude = active_backend_id() == "openrouter"
    model = _model(payload.get("model"), prefix_claude=prefix_claude)
    messages = _messages(payload.get("messages"))
    out: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": _stream(payload),
    }
    out["max_tokens"] = _positive_int(payload.get("max_tokens"), "max_tokens")
    _copy_sampling(payload, out)
    tools = _tools(payload.get("tools"))
    if tools:
        out["tools"] = tools
    if "tool_choice" in payload and payload["tool_choice"] is not None:
        out["tool_choice"] = _tool_choice(payload["tool_choice"])
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    return out


def to_anthropic_message(completion: dict[str, Any], *, fallback_model: str) -> dict[str, Any]:
    """Turn one non-stream chat completion into an Anthropic message."""
    if not isinstance(completion, dict):
        raise _bad_completion()
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _bad_completion()
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _bad_completion()
    content = _assistant_blocks(message)
    if not content:
        content = [{"type": "text", "text": ""}]
    usage = completion.get("usage") if isinstance(completion.get("usage"), dict) else {}
    model = completion.get("model")
    return {
        "id": message_id(completion.get("id")),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model if isinstance(model, str) and model else fallback_model,
        "stop_reason": stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": _token_count(usage.get("prompt_tokens")),
            "output_tokens": _token_count(usage.get("completion_tokens")),
        },
    }


def parse_completion_body(body: bytes, *, fallback_model: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _bad_completion() from None
    return to_anthropic_message(payload, fallback_model=fallback_model)


def anthropic_error_from_gateway(exc: GatewayError) -> tuple[int, dict[str, Any]]:
    """Anthropic `{type: error}` body. Bridge auth stays 401; upstream auth does not."""
    status = exc.status_code
    message = exc.message
    upstream = _upstream_block(exc.extra)
    upstream_status: int | None = None
    if isinstance(upstream, dict):
        raw_status = upstream.get("upstream_status")
        if isinstance(raw_status, int) and 400 <= raw_status <= 599:
            upstream_status = raw_status
            status = raw_status
        extracted = _openai_error_message(upstream.get("upstream_body"))
        if extracted:
            message = extracted
    if upstream_status in {401, 403}:
        status = 502
        label = _LABELS.get(
            str(upstream.get("backend") if isinstance(upstream, dict) else ""), "Upstream"
        )
        if label.lower() not in message.lower():
            message = f"{label} rejected the upstream credential: " + message
        err_type = "api_error"
    elif status == 401:
        err_type = "authentication_error"
    elif status == 403:
        err_type = "permission_error"
    elif status == 404:
        err_type = "not_found_error"
    elif status == 413:
        err_type = "request_too_large"
    elif status == 429:
        err_type = "rate_limit_error"
    elif status == 529:
        err_type = "overloaded_error"
    elif 400 <= status < 500:
        err_type = "invalid_request_error"
    else:
        err_type = "api_error"
    hint = exc.as_dict()["error"]["hint"]
    return status, {
        "type": "error",
        "error": {"type": err_type, "message": message, "hint": hint},
    }


def message_to_sse(message: dict[str, Any]) -> bytes:
    """One Anthropic message as the SSE sequence Claude Code expects."""
    parts: list[bytes] = [
        sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message["id"],
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": message["model"],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": message["usage"]["input_tokens"],
                        "output_tokens": 0,
                    },
                },
            },
        )
    ]
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            parts.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            if block["text"]:
                parts.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": block["text"]},
                        },
                    )
                )
        else:
            parts.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": {},
                        },
                    },
                )
            )
            encoded = json.dumps(block["input"], ensure_ascii=False, separators=(",", ":"))
            parts.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": encoded},
                    },
                )
            )
        parts.append(
            sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        )
    parts.append(
        sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": message["stop_reason"],
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": message["usage"]["output_tokens"]},
            },
        )
    )
    parts.append(sse_event("message_stop", {"type": "message_stop"}))
    return b"".join(parts)


def sse_event(name: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n".encode()


def message_id(raw: object) -> str:
    if isinstance(raw, str) and raw:
        if raw.startswith("msg_"):
            return raw
        return "msg_" + raw.removeprefix("chatcmpl-")
    return "msg_os3"


def stop_reason(finish: object) -> str:
    if isinstance(finish, str):
        return _STOP_REASONS.get(finish, "end_turn")
    return "end_turn"


class AnthropicSseTranslator:
    """Buffer OpenAI chat SSE and emit Anthropic message events."""

    def __init__(self, fallback_model: str) -> None:
        self._fallback_model = fallback_model
        self._raw = b""
        self._buf = ""
        self._started = False
        self._done = False
        self._id = "msg_os3"
        self._model = fallback_model
        self._next = 0
        self._text_index: int | None = None
        self._text_open = False
        self._tools: dict[int, int] = {}
        self._finish: str | None = None
        self._input_tokens = 0
        self._output_tokens = 0

    @property
    def closed(self) -> bool:
        return self._done

    def feed(self, chunk: bytes) -> list[bytes]:
        if self._done or not chunk:
            return []
        self._raw += chunk
        try:
            text = self._raw.decode("utf-8")
        except UnicodeDecodeError:
            if len(self._raw) > 1_048_576:
                return [self.fail("Upstream stream was not valid UTF-8")]
            return []
        self._raw = b""
        self._buf += text.replace("\r\n", "\n").replace("\r", "\n")
        out: list[bytes] = []
        while "\n\n" in self._buf:
            event, self._buf = self._buf.split("\n\n", 1)
            out.extend(self._consume(event))
            if self._done:
                break
        return out

    def finish(self) -> list[bytes]:
        if self._done:
            return []
        out: list[bytes] = []
        tail = self._buf
        self._buf = ""
        if tail.strip():
            out.extend(self._consume(tail))
        if not self._done:
            out.extend(self._close())
        return out

    def fail(self, message: str, hint: str | None = None) -> bytes:
        self._done = True
        error = {"type": "api_error", "message": message}
        if hint:
            error["hint"] = hint
        return sse_event("error", {"type": "error", "error": error})

    def _consume(self, raw: str) -> list[bytes]:
        if self._done:
            return []
        data_lines: list[str] = []
        for line in raw.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return []
        data = "\n".join(data_lines)
        if data == "[DONE]":
            return self._close()
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        error = payload.get("error")
        if isinstance(error, dict):
            hint = error.get("hint")
            return [
                self.fail(
                    str(error.get("message") or "Upstream failed"),
                    hint if isinstance(hint, str) else None,
                )
            ]
        return self._on_chunk(payload)

    def _on_chunk(self, payload: dict[str, Any]) -> list[bytes]:
        model = payload.get("model")
        if isinstance(model, str) and model:
            self._model = model
        openai_id = payload.get("id")
        if isinstance(openai_id, str) and openai_id:
            self._id = message_id(openai_id)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            self._input_tokens = _token_count(prompt, default=self._input_tokens)
            self._output_tokens = _token_count(completion, default=self._output_tokens)
        out: list[bytes] = []
        if not self._started:
            out.extend(self._start())
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return out
        choice = choices[0]
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            self._finish = finish
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return out
        content = delta.get("content")
        if isinstance(content, str) and content:
            out.extend(self._text(content))
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if isinstance(call, dict):
                    out.extend(self._tool(call))
        return out

    def _start(self) -> list[bytes]:
        self._started = True
        return [
            sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self._id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": self._model or self._fallback_model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": self._input_tokens, "output_tokens": 0},
                    },
                },
            )
        ]

    def _text(self, text: str) -> list[bytes]:
        out: list[bytes] = []
        if not self._text_open:
            index = self._next
            self._next += 1
            self._text_index = index
            self._text_open = True
            out.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        out.append(
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return out

    def _tool(self, call: dict[str, Any]) -> list[bytes]:
        index = call.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            index = 0
        out: list[bytes] = []
        if index not in self._tools:
            out.extend(self._close_text())
            block_index = self._next
            self._next += 1
            self._tools[index] = block_index
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name") if isinstance(function, dict) else ""
            tool_id = call.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                tool_id = f"toolu_{block_index}"
            out.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": name if isinstance(name, str) else "",
                            "input": {},
                        },
                    },
                )
            )
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if isinstance(arguments, str) and arguments:
            out.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._tools[index],
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    },
                )
            )
        return out

    def _close_text(self) -> list[bytes]:
        if not self._text_open or self._text_index is None:
            return []
        index = self._text_index
        self._text_open = False
        self._text_index = None
        return [
            sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        ]

    def _close(self) -> list[bytes]:
        if self._done:
            return []
        self._done = True
        out: list[bytes] = []
        if not self._started:
            out.extend(self._start())
        out.extend(self._close_text())
        for block_index in self._tools.values():
            out.append(
                sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": block_index},
                )
            )
        self._tools.clear()
        out.append(
            sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": stop_reason(self._finish),
                        "stop_sequence": None,
                    },
                    "usage": {
                        "input_tokens": self._input_tokens,
                        "output_tokens": self._output_tokens,
                    },
                },
            )
        )
        out.append(sse_event("message_stop", {"type": "message_stop"}))
        return out


def _native_model(raw: object) -> str:
    if raw is None:
        return model_for("anthropic")
    if not isinstance(raw, str) or not raw.strip():
        raise GatewayError(
            400,
            "model must be a non-empty string",
            "invalid_request_error",
            "invalid_model",
        )
    model = raw.strip()
    if model.startswith("anthropic/"):
        return model.split("/", 1)[1]
    return model


def _model(raw: object, *, prefix_claude: bool) -> str:
    if raw is None:
        return default_model()
    if not isinstance(raw, str) or not raw.strip():
        raise GatewayError(
            400,
            "model must be a non-empty string",
            "invalid_request_error",
            "invalid_model",
        )
    model = raw.strip()
    if prefix_claude and "/" not in model and model.lower().startswith("claude"):
        return "anthropic/" + model
    return model


def _upstream_block(extra: dict[str, Any]) -> dict[str, Any] | None:
    block = extra.get("upstream")
    if isinstance(block, dict):
        return block
    legacy = extra.get("openrouter")
    if isinstance(legacy, dict):
        merged = dict(legacy)
        merged.setdefault("backend", "openrouter")
        return merged
    return None


def _stream(payload: dict[str, Any]) -> bool:
    if "stream" not in payload or payload["stream"] is None:
        return False
    stream = payload["stream"]
    if not isinstance(stream, bool):
        raise GatewayError(
            400,
            "stream must be a boolean",
            "invalid_request_error",
            "invalid_stream",
        )
    return stream


def _messages(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or any(not isinstance(item, dict) for item in raw):
        raise GatewayError(
            400,
            "messages must be a non-empty list of objects",
            "invalid_request_error",
            "invalid_messages",
        )
    system_from_role: list[str] = []
    converted: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for item in raw:
        role = item.get("role")
        if role == "system":
            text = _text_of(item.get("content"))
            if text:
                system_from_role.append(text)
            continue
        if role == "user":
            converted.extend(_user_message(item.get("content"), tool_names))
            continue
        if role == "assistant":
            converted.append(_assistant_message(item.get("content"), tool_names))
            continue
        raise GatewayError(
            400,
            "message role must be user or assistant",
            "invalid_request_error",
            "invalid_messages",
        )
    if not converted:
        raise GatewayError(
            400,
            "messages must include a user or assistant turn",
            "invalid_request_error",
            "invalid_messages",
        )
    system_messages = [{"role": "system", "content": text} for text in system_from_role]
    return [*system_messages, *converted]


def _user_message(content: object, tool_names: dict[str, str]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        raise GatewayError(
            400,
            "message content must be a string or a list of blocks",
            "invalid_request_error",
            "invalid_messages",
        )
    tools: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise GatewayError(
                400,
                "content blocks must be objects",
                "invalid_request_error",
                "invalid_messages",
            )
        kind = block.get("type")
        if kind in {"thinking", "redacted_thinking"}:
            continue
        if kind == "tool_result":
            tools.append(_tool_result(block, tool_names))
            continue
        if kind == "image":
            parts.append(_image(block))
            continue
        if kind == "text" or kind is None:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append({"type": "text", "text": text})
    out: list[dict[str, Any]] = list(tools)
    if parts:
        if len(parts) == 1 and parts[0]["type"] == "text":
            out.append({"role": "user", "content": parts[0]["text"]})
        else:
            out.append({"role": "user", "content": parts})
    elif not tools:
        out.append({"role": "user", "content": ""})
    return out


def _assistant_message(content: object, tool_names: dict[str, str]) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    if not isinstance(content, list):
        raise GatewayError(
            400,
            "message content must be a string or a list of blocks",
            "invalid_request_error",
            "invalid_messages",
        )
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise GatewayError(
                400,
                "content blocks must be objects",
                "invalid_request_error",
                "invalid_messages",
            )
        kind = block.get("type")
        if kind in {"thinking", "redacted_thinking"}:
            continue
        if kind == "tool_use":
            tool_calls.append(_tool_use(block, tool_names))
            continue
        if kind == "text" or kind is None:
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    elif message["content"] is None:
        message["content"] = ""
    return message


def _tool_use(block: dict[str, Any], tool_names: dict[str, str]) -> dict[str, Any]:
    name = block.get("name")
    tool_id = block.get("id")
    if not isinstance(name, str) or not name:
        raise GatewayError(
            400,
            "tool_use.name is required",
            "invalid_request_error",
            "invalid_tool_use",
        )
    if not isinstance(tool_id, str) or not tool_id:
        raise GatewayError(
            400,
            "tool_use.id is required",
            "invalid_request_error",
            "invalid_tool_use",
        )
    tool_names[tool_id] = name
    raw_input = block.get("input")
    arguments = raw_input if isinstance(raw_input, dict) else {}
    return {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
        },
    }


def _tool_result(block: dict[str, Any], tool_names: dict[str, str]) -> dict[str, Any]:
    tool_id = block.get("tool_use_id")
    if not isinstance(tool_id, str) or not tool_id:
        raise GatewayError(
            400,
            "tool_result.tool_use_id is required",
            "invalid_request_error",
            "invalid_tool_result",
        )
    text = _flatten_tool_content(block.get("content"))
    if block.get("is_error") is True:
        text = "Tool error:\n" + text
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tool_id,
        "content": text,
    }
    name = tool_names.get(tool_id)
    if name:
        message["name"] = name
    return message


def _flatten_tool_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _image(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise GatewayError(
            400,
            "image.source is required",
            "invalid_request_error",
            "invalid_image",
        )
    kind = source.get("type")
    if kind == "base64":
        media = source.get("media_type")
        data = source.get("data")
        if not isinstance(media, str) or not media or not isinstance(data, str) or not data:
            raise GatewayError(
                400,
                "image base64 source needs media_type and data",
                "invalid_request_error",
                "invalid_image",
            )
        url = f"data:{media};base64,{data}"
    elif kind == "url":
        url_value = source.get("url")
        if not isinstance(url_value, str) or not url_value:
            raise GatewayError(
                400,
                "image url source needs url",
                "invalid_request_error",
                "invalid_image",
            )
        url = url_value
    else:
        raise GatewayError(
            400,
            "image.source.type must be base64 or url",
            "invalid_request_error",
            "invalid_image",
        )
    return {"type": "image_url", "image_url": {"url": url}}


def _tools(raw: object) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise GatewayError(
            400,
            "tools must be a list",
            "invalid_request_error",
            "invalid_tools",
        )
    converted: list[dict[str, Any]] = []
    for tool in raw:
        if not isinstance(tool, dict):
            raise GatewayError(
                400,
                "tools must be objects",
                "invalid_request_error",
                "invalid_tools",
            )
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise GatewayError(
                400,
                "tool.name is required",
                "invalid_request_error",
                "invalid_tools",
            )
        schema = tool.get("input_schema")
        if schema is None:
            schema = {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise GatewayError(
                400,
                "tool.input_schema must be an object",
                "invalid_request_error",
                "invalid_tools",
            )
        function: dict[str, Any] = {"name": name, "parameters": schema}
        description = tool.get("description")
        if isinstance(description, str):
            function["description"] = description
        converted.append({"type": "function", "function": function})
    return converted


def _tool_choice(raw: object) -> Any:
    if isinstance(raw, str):
        if raw in {"auto", "none", "required"}:
            return raw
        raise GatewayError(
            400,
            "tool_choice is invalid",
            "invalid_request_error",
            "invalid_tool_choice",
        )
    if not isinstance(raw, dict):
        raise GatewayError(
            400,
            "tool_choice is invalid",
            "invalid_request_error",
            "invalid_tool_choice",
        )
    kind = raw.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise GatewayError(
                400,
                "tool_choice.tool needs a name",
                "invalid_request_error",
                "invalid_tool_choice",
            )
        return {"type": "function", "function": {"name": name}}
    raise GatewayError(
        400,
        "tool_choice.type is invalid",
        "invalid_request_error",
        "invalid_tool_choice",
    )


def _copy_sampling(payload: dict[str, Any], out: dict[str, Any]) -> None:
    system = _system_text(payload.get("system"))
    if system:
        out["messages"] = [{"role": "system", "content": system}, *out["messages"]]
    if "temperature" in payload and payload["temperature"] is not None:
        out["temperature"] = _number(payload["temperature"], "temperature")
    if "top_p" in payload and payload["top_p"] is not None:
        out["top_p"] = _number(payload["top_p"], "top_p")
    if "stop_sequences" in payload and payload["stop_sequences"] is not None:
        stops = payload["stop_sequences"]
        if not isinstance(stops, list) or any(not isinstance(item, str) for item in stops):
            raise GatewayError(
                400,
                "stop_sequences must be a list of strings",
                "invalid_request_error",
                "invalid_stop_sequences",
            )
        if stops:
            out["stop"] = stops


def _system_text(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, str) and block.strip():
                parts.append(block.strip())
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    raise GatewayError(
        400,
        "system must be a string or a list of text blocks",
        "invalid_request_error",
        "invalid_system",
    )


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    return _system_text(content)


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    blocks.append({"type": "text", "text": text})
    for call in _completion_calls(message):
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") if isinstance(function, dict) else ""
        raw_args = function.get("arguments") if isinstance(function, dict) else "{}"
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") if isinstance(call.get("id"), str) else "toolu_missing",
                "name": name if isinstance(name, str) else "",
                "input": _object_args(raw_args),
            }
        )
    return blocks


def _completion_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("tool_calls")
    if isinstance(raw, list) and raw:
        return [call for call in raw if isinstance(call, dict)]
    legacy = message.get("function_call")
    if not isinstance(legacy, dict):
        return []
    name = legacy.get("name") if isinstance(legacy.get("name"), str) else ""
    arguments = legacy.get("arguments") if isinstance(legacy.get("arguments"), str) else "{}"
    return [
        {
            "id": "call_function",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
    ]


def _object_args(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _openai_error_message(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:500]
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GatewayError(
            400,
            f"{field} must be a positive integer",
            "invalid_request_error",
            "invalid_request",
        )
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewayError(
            400,
            f"{field} must be a number",
            "invalid_request_error",
            "invalid_request",
        )
    return float(value)


def _token_count(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _bad_completion() -> GatewayError:
    return GatewayError(
        502,
        "OpenRouter returned an unusable completion",
        "upstream_error",
        "openrouter_bad_body",
    )
