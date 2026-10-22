"""OpenAI chat completions ↔ Anthropic Messages.

Used when NEXUS_BACKEND=anthropic and the client called /v1/chat/completions.
Native POST /v1/messages does not use this module; it forwards the Messages body.

Thinking blocks are dropped on this path. They stay on the native Messages path.
"""

from __future__ import annotations

import json
import time
from typing import Any

from nexus.config import model_for
from nexus.errors import GatewayError

_FINISH = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}
_CHAT_MAX_TOKENS = 4096


def openai_chat_to_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    """One OpenAI chat body → one Anthropic Messages body."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise GatewayError(
            400,
            "messages must be a list of objects",
            "invalid_request_error",
            "invalid_messages",
        )
    system, converted = _convert_messages(messages)
    out: dict[str, Any] = {
        "model": _anthropic_model(payload.get("model")),
        "max_tokens": _max_tokens(payload),
        "messages": converted or [{"role": "user", "content": ""}],
        "stream": bool(payload.get("stream")),
    }
    if system:
        out["system"] = system
    if "temperature" in payload and payload["temperature"] is not None:
        out["temperature"] = _number(payload["temperature"], "temperature")
    if "top_p" in payload and payload["top_p"] is not None:
        out["top_p"] = _number(payload["top_p"], "top_p")
    stops = _stop_sequences(payload.get("stop"))
    if stops:
        out["stop_sequences"] = stops
    tools = _tools(_tool_definitions(payload))
    if tools:
        out["tools"] = tools
    if "tool_choice" in payload and payload["tool_choice"] is not None:
        out["tool_choice"] = _tool_choice(payload["tool_choice"])
    return out


def anthropic_message_to_completion(
    message: dict[str, Any], *, fallback_model: str
) -> dict[str, Any]:
    """One Anthropic message → one OpenAI chat completion."""
    if not isinstance(message, dict) or message.get("type") not in {None, "message"}:
        raise _bad_message()
    content = message.get("content")
    if content is None:
        content = []
    if not isinstance(content, list):
        raise _bad_message()
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"thinking", "redacted_thinking"}:
            continue
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif kind == "tool_use":
            tool_calls.append(_tool_call(block))
    assistant: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        assistant["tool_calls"] = tool_calls
    elif assistant["content"] is None:
        assistant["content"] = ""
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    prompt = _token_count(usage.get("input_tokens"))
    completion = _token_count(usage.get("output_tokens"))
    model = message.get("model")
    finish = _FINISH.get(message.get("stop_reason"), "stop")
    if tool_calls and message.get("stop_reason") in {None, "end_turn"}:
        finish = "tool_calls"
    return {
        "id": _completion_id(message.get("id")),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model if isinstance(model, str) and model else fallback_model,
        "choices": [{"index": 0, "message": assistant, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


class OpenAISseFromAnthropic:
    """Buffer Anthropic message SSE and emit OpenAI chat chunks."""

    def __init__(self, fallback_model: str) -> None:
        self._fallback_model = fallback_model
        self._raw = b""
        self._buf = ""
        self._done = False
        self._started = False
        self._id = "chatcmpl-os3"
        self._model = fallback_model
        self._created = int(time.time())
        self._tools: dict[int, int] = {}
        self._next_tool = 0
        self._finish = "stop"
        self._input_tokens = 0
        self._output_tokens = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        if self._done or not chunk:
            return []
        self._raw += chunk
        try:
            text = self._raw.decode("utf-8")
        except UnicodeDecodeError:
            if len(self._raw) > 1_048_576:
                return self._fail("Upstream stream was not valid UTF-8")
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

    def _fail(self, message: str) -> list[bytes]:
        out = self._text(message)
        out.extend(self._close())
        return out

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
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        return self._on_event(payload)

    def _on_event(self, payload: dict[str, Any]) -> list[bytes]:
        kind = payload.get("type")
        if kind == "ping":
            return []
        if kind == "error":
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            message = error.get("message") if isinstance(error, dict) else "Upstream error"
            return self._fail(message if isinstance(message, str) else "Upstream error")
        if kind == "message_start":
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            self._id = _completion_id(message.get("id"))
            model = message.get("model")
            if isinstance(model, str) and model:
                self._model = model
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            self._input_tokens = _token_count(usage.get("input_tokens"), default=self._input_tokens)
            return []
        if kind == "content_block_start":
            return self._block_start(payload)
        if kind == "content_block_delta":
            return self._block_delta(payload)
        if kind == "message_delta":
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            reason = delta.get("stop_reason")
            if isinstance(reason, str):
                self._finish = _FINISH.get(reason, "stop")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            self._output_tokens = _token_count(
                usage.get("output_tokens"),
                default=self._output_tokens,
            )
            if "input_tokens" in usage:
                self._input_tokens = _token_count(
                    usage.get("input_tokens"), default=self._input_tokens
                )
            return []
        if kind == "message_stop":
            return self._close()
        return []

    def _block_start(self, payload: dict[str, Any]) -> list[bytes]:
        block = (
            payload.get("content_block") if isinstance(payload.get("content_block"), dict) else {}
        )
        index = payload.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            index = 0
        if block.get("type") != "tool_use":
            return []
        tool_index = self._next_tool
        self._next_tool += 1
        self._tools[index] = tool_index
        name = block.get("name") if isinstance(block.get("name"), str) else ""
        tool_id = block.get("id") if isinstance(block.get("id"), str) else f"toolu_{tool_index}"
        call = {
            "index": tool_index,
            "id": tool_id,
            "type": "function",
            "function": {"name": name, "arguments": ""},
        }
        return [self._chunk({"tool_calls": [call]})]

    def _block_delta(self, payload: dict[str, Any]) -> list[bytes]:
        delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        kind = delta.get("type")
        if kind == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                return self._text(text)
            return []
        if kind == "input_json_delta":
            partial = delta.get("partial_json")
            if not isinstance(partial, str) or not partial:
                return []
            index = payload.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                index = 0
            tool_index = self._tools.get(index, 0)
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {"index": tool_index, "function": {"arguments": partial}},
                        ]
                    }
                )
            ]
        return []

    def _text(self, text: str) -> list[bytes]:
        return [self._chunk({"content": text})]

    def _chunk(
        self, delta: dict[str, Any], finish: str | None = None, *, usage: bool = False
    ) -> bytes:
        if not self._started:
            self._started = True
            if "role" not in delta:
                delta = {"role": "assistant", **delta}
        body: dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model or self._fallback_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            body["usage"] = {
                "prompt_tokens": self._input_tokens,
                "completion_tokens": self._output_tokens,
                "total_tokens": self._input_tokens + self._output_tokens,
            }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        return f"data: {encoded}\n\n".encode()

    def _close(self) -> list[bytes]:
        if self._done:
            return []
        self._done = True
        out: list[bytes] = []
        if not self._started:
            out.append(self._chunk({"role": "assistant", "content": ""}))
        out.append(self._chunk({}, self._finish, usage=True))
        out.append(b"data: [DONE]\n\n")
        return out


def _anthropic_model(raw: object) -> str:
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


def _max_tokens(payload: dict[str, Any]) -> int:
    for field in ("max_tokens", "max_completion_tokens"):
        if field not in payload or payload[field] is None:
            continue
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GatewayError(
                400,
                f"{field} must be a positive integer",
                "invalid_request_error",
                "invalid_request",
            )
        return value
    return _CHAT_MAX_TOKENS


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewayError(
            400,
            f"{field} must be a number",
            "invalid_request_error",
            "invalid_request",
        )
    return float(value)


def _stop_sequences(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return [item for item in raw if item]
    raise GatewayError(
        400,
        "stop must be a string or a list of strings",
        "invalid_request_error",
        "invalid_stop",
    )


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for item in messages:
        role = item.get("role")
        if role == "system":
            text = _flatten_text(item.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            _add_user(converted, _user_content(item.get("content")))
            continue
        if role == "assistant":
            converted.append({"role": "assistant", "content": _assistant_content(item)})
            continue
        if role == "tool":
            _add_user(converted, [_tool_result(item)])
            continue
        raise GatewayError(
            400,
            "message role must be system, user, assistant, or tool",
            "invalid_request_error",
            "invalid_messages",
        )
    return "\n".join(system_parts), converted


def _add_user(messages: list[dict[str, Any]], content: Any) -> None:
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = _merge(messages[-1]["content"], content)
        return
    messages.append({"role": "user", "content": content})


def _merge(left: Any, right: Any) -> list[dict[str, Any]]:
    return [*_as_blocks(left), *_as_blocks(right)]


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _user_content(content: object) -> Any:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise GatewayError(
            400,
            "message content must be a string or a list of parts",
            "invalid_request_error",
            "invalid_messages",
        )
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            raise GatewayError(
                400,
                "message content parts must be objects",
                "invalid_request_error",
                "invalid_messages",
            )
        kind = part.get("type")
        if kind in {None, "text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            continue
        if kind in {"image_url", "input_image"}:
            blocks.append(_image(part.get("image_url")))
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return blocks[0]["text"]
    return blocks


def _assistant_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str) and part:
                blocks.append({"type": "text", "text": part})
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    blocks.append({"type": "text", "text": text})
    for call in _calls(message):
        blocks.append(_tool_use(call))
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def _tool_use(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = function.get("name") if isinstance(function, dict) else ""
    raw_args = function.get("arguments") if isinstance(function, dict) else "{}"
    tool_id = call.get("id") if isinstance(call.get("id"), str) else "toolu_missing"
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": name if isinstance(name, str) else "",
        "input": _object_args(raw_args),
    }


def _tool_result(message: dict[str, Any]) -> dict[str, Any]:
    tool_id = message.get("tool_call_id")
    if not isinstance(tool_id, str) or not tool_id:
        raise GatewayError(
            400,
            "tool message tool_call_id is required",
            "invalid_request_error",
            "invalid_tool_result",
        )
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": _flatten_text(message.get("content")),
    }


def _image(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        url = raw
    elif isinstance(raw, dict) and isinstance(raw.get("url"), str):
        url = raw["url"]
    else:
        raise GatewayError(
            400,
            "image_url is required",
            "invalid_request_error",
            "invalid_image",
        )
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        meta = header[5:]
        media, sep, encoding = meta.partition(";")
        if sep != ";" or encoding != "base64" or not media or not data:
            raise GatewayError(
                400,
                "image data URL must be data:<media>;base64,<data>",
                "invalid_request_error",
                "invalid_image",
            )
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _tool_definitions(payload: dict[str, Any]) -> object:
    tools = payload.get("tools")
    if tools is not None:
        return tools
    functions = payload.get("functions")
    if not isinstance(functions, list):
        return None
    wrapped: list[dict[str, Any]] = []
    for item in functions:
        if isinstance(item, dict) and "function" not in item:
            wrapped.append({"type": "function", "function": item})
        else:
            wrapped.append(item)
    return wrapped


def _calls(message: dict[str, Any]) -> list[dict[str, Any]]:
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
        if tool.get("type") not in {None, "function"}:
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            raise GatewayError(
                400,
                "tool function name is required",
                "invalid_request_error",
                "invalid_tools",
            )
        parameters = function.get("parameters") if isinstance(function, dict) else None
        if parameters is None:
            parameters = {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise GatewayError(
                400,
                "tool parameters must be an object",
                "invalid_request_error",
                "invalid_tools",
            )
        row: dict[str, Any] = {"name": name, "input_schema": parameters}
        description = function.get("description") if isinstance(function, dict) else None
        if isinstance(description, str):
            row["description"] = description
        converted.append(row)
    return converted or None


def _tool_choice(raw: object) -> dict[str, Any]:
    if raw == "auto":
        return {"type": "auto"}
    if raw == "none":
        return {"type": "none"}
    if raw == "required":
        return {"type": "any"}
    if isinstance(raw, dict):
        if raw.get("type") == "function":
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name:
                return {"type": "tool", "name": name}
        kind = raw.get("type")
        if kind in {"auto", "any", "none", "tool"}:
            return raw
    raise GatewayError(
        400,
        "tool_choice is invalid",
        "invalid_request_error",
        "invalid_tool_choice",
    )


def _tool_call(block: dict[str, Any]) -> dict[str, Any]:
    name = block.get("name") if isinstance(block.get("name"), str) else ""
    tool_id = block.get("id") if isinstance(block.get("id"), str) else "toolu_missing"
    return {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                block.get("input") if isinstance(block.get("input"), dict) else {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    }


def _flatten_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _object_args(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"raw": parsed}


def _completion_id(raw: object) -> str:
    if isinstance(raw, str) and raw:
        if raw.startswith("chatcmpl-"):
            return raw
        return "chatcmpl-" + raw.removeprefix("msg_")
    return "chatcmpl-os3"


def _token_count(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _bad_message() -> GatewayError:
    return GatewayError(
        502,
        "Anthropic returned an unusable message",
        "upstream_error",
        "anthropic_bad_body",
    )
