"""OpenAI chat completions <-> the ChatGPT Codex Responses API.

``chat_to_responses`` turns one chat-completions body (messages, tools, tool_choice,
images, reasoning_effort) into a Responses body. ``ChatSseFromResponses`` turns the
Responses event stream back into chat-completions SSE chunks, and
``completion_from_events`` folds the same events into one non-stream completion.
Pure functions: no I/O, no credentials.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"text", "input_text"} and isinstance(part.get("text"), str):
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url:
                item: dict[str, Any] = {"type": "input_image", "image_url": url}
                if isinstance(image, dict) and image.get("detail") in {"low", "high", "auto"}:
                    item["detail"] = image["detail"]
                parts.append(item)
    return parts


def _tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict) or tool.get("type", "function") != "function":
        return None
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    out: dict[str, Any] = {
        "type": "function",
        "name": name,
        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        "strict": bool(fn.get("strict", False)),
    }
    if isinstance(fn.get("description"), str):
        out["description"] = fn["description"]
    return out


def _tool_choice(choice: Any) -> Any:
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        return choice
    if isinstance(choice, dict):
        fn = choice.get("function") if isinstance(choice.get("function"), dict) else choice
        if isinstance(fn.get("name"), str):
            return {"type": "function", "name": fn["name"]}
    return "auto"


def chat_to_responses(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _text_of(content)
            if text:
                instructions.append(text)
        elif role == "user":
            parts = _user_parts(content)
            if parts:
                items.append({"type": "message", "role": "user", "content": parts})
        elif role == "assistant":
            text = _text_of(content)
            if text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args if args is not None else {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                        "name": str(fn.get("name") or ""),
                        "arguments": args,
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": _text_of(content),
                }
            )
    body: dict[str, Any] = {
        "model": model,
        "instructions": "\n\n".join(instructions) or DEFAULT_INSTRUCTIONS,
        "input": items,
        "store": False,
        "stream": True,
        "include": [],
    }
    tools = [t for t in (_tool(x) for x in payload.get("tools") or []) if t is not None]
    if tools:
        body["tools"] = tools
        body["tool_choice"] = _tool_choice(payload.get("tool_choice"))
        body["parallel_tool_calls"] = bool(payload.get("parallel_tool_calls", True))
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str) and effort in {"minimal", "low", "medium", "high", "xhigh"}:
        body["reasoning"] = {"effort": effort}
    fmt = payload.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        schema = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else {}
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": str(schema.get("name") or "response"),
                "schema": schema.get("schema") or {},
                "strict": bool(schema.get("strict", False)),
            }
        }
    elif isinstance(fmt, dict) and fmt.get("type") == "json_object":
        body["text"] = {"format": {"type": "json_object"}}
    return body


def _usage(response: dict[str, Any] | None) -> dict[str, int] | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def parse_sse(buffer: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """Complete ``data:`` events in ``buffer`` and the unfinished remainder."""
    events: list[dict[str, Any]] = []
    buffer = buffer.replace(b"\r\n", b"\n")
    while b"\n\n" in buffer:
        block, buffer = buffer.split(b"\n\n", 1)
        data = b"\n".join(
            line[5:].lstrip() for line in block.split(b"\n") if line.startswith(b"data:")
        )
        if not data or data.strip() == b"[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, buffer


class ResponsesFailed(Exception):
    """The stream ended in ``response.failed`` or ``error``. ``event`` is the raw event."""

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__("response failed")
        self.event = event


def failure_detail(event: dict[str, Any]) -> tuple[str, str]:
    """(code, message) of a failed-response or error event. Never contains credentials."""
    error = event.get("error")
    if not isinstance(error, dict):
        response = event.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        error = {}
    code = str(error.get("code") or error.get("type") or event.get("code") or "response_failed")
    message = str(error.get("message") or event.get("message") or "The ChatGPT response failed.")
    return code, message


class ChatSseFromResponses:
    """Feed raw Responses SSE bytes; get chat-completions ``data:`` lines back."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self._buffer = b""
        self._started = False
        self._calls: dict[str, int] = {}
        self._args_seen: set[str] = set()
        self.done = False

    def _chunk(
        self,
        delta: dict[str, Any],
        finish: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> bytes:
        body: dict[str, Any] = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            body["usage"] = usage
        return b"data: " + json.dumps(body, ensure_ascii=False).encode() + b"\n\n"

    def _args(self, index: int, text: str) -> bytes:
        return self._chunk({"tool_calls": [{"index": index, "function": {"arguments": text}}]})

    def _start(self) -> list[bytes]:
        if self._started:
            return []
        self._started = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def feed(self, raw: bytes) -> list[bytes]:
        events, self._buffer = parse_sse(self._buffer + raw)
        out: list[bytes] = []
        for event in events:
            out.extend(self.event(event))
        return out

    def event(self, event: dict[str, Any]) -> list[bytes]:
        kind = event.get("type")
        if kind == "response.created":
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("model"), str):
                self.model = response["model"]
            return self._start()
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                return [*self._start(), self._chunk({"content": delta})]
            return []
        if kind == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                key = str(item.get("id") or item.get("call_id"))
                index = len(self._calls)
                self._calls[key] = index
                call = {
                    "index": index,
                    "id": str(item.get("call_id") or key),
                    "type": "function",
                    "function": {"name": str(item.get("name") or ""), "arguments": ""},
                }
                return [*self._start(), self._chunk({"tool_calls": [call]})]
            return []
        if kind == "response.function_call_arguments.delta":
            key = str(event.get("item_id"))
            index = self._calls.get(key)
            delta = event.get("delta")
            if index is None or not isinstance(delta, str) or not delta:
                return []
            self._args_seen.add(key)
            return [self._args(index, delta)]
        if kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                key = str(item.get("id") or item.get("call_id"))
                index = self._calls.get(key)
                args = item.get("arguments")
                if index is not None and key not in self._args_seen and isinstance(args, str):
                    self._args_seen.add(key)
                    return [self._args(index, args)]
            return []
        if kind in {"response.completed", "response.incomplete"}:
            self.done = True
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            finish = "tool_calls" if self._calls else "stop"
            if kind == "response.incomplete":
                finish = "length"
            return [*self._start(), self._chunk({}, finish, _usage(response))]
        if kind in {"response.failed", "error"}:
            raise ResponsesFailed(event)
        return []


def completion_from_events(events: list[dict[str, Any]], *, model: str) -> dict[str, Any]:
    """One chat completion from a full list of Responses events."""
    final: dict[str, Any] | None = None
    text: list[str] = []
    # Items as they finished, in order. The final response's ``output`` is
    # sometimes empty even though function calls were streamed, so these are
    # the fallback when it is.
    done_items: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind in {"response.failed", "error"}:
            raise ResponsesFailed(event)
        if kind in {"response.completed", "response.incomplete"}:
            final = event.get("response") if isinstance(event.get("response"), dict) else {}
        elif kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text.append(event["delta"])
        elif kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            done_items.append(event["item"])
    output = (final or {}).get("output") or done_items
    calls: list[dict[str, Any]] = []
    message_text: list[str] = []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id")),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    message_text.append(str(part.get("text") or ""))
    content = "".join(message_text) or "".join(text)
    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = calls
    finish = "tool_calls" if calls else "stop"
    if final is not None and (final.get("status") == "incomplete"):
        finish = "length"
    body: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str((final or {}).get("model") or model),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    usage = _usage(final)
    if usage is not None:
        body["usage"] = usage
    return body
