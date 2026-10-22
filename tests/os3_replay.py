"""Offline OS3 replay: OS3-shaped requests through the real app, a scripted fake upstream.

No network, no quota. The fake upstream answers the way weak models do, including
malformed tool calls, as plain JSON or as SSE cut at awkward byte boundaries. A
separate oracle (`os3_rejects`) models what OS3 does with the answer, and shares no
code with the bridge's validator. `python -m tests.os3_replay` prints the
before/after table; tests/test_os3_replay.py asserts it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend

from tests.transport import HandlerTransport

jsonschema = pytest.importorskip("jsonschema")
Draft202012Validator = jsonschema.Draft202012Validator

FIXTURES = Path(__file__).parent / "fixtures" / "os3"
MODEL = "meituan/longcat-2.5-preview:free"
SCRIPTS = {"probe.py", "capture.py", "act.py"}


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Call:
    name: str
    arguments: Any
    id: str | None = "call_m1"


@dataclass
class Reply:
    calls: list[Call] = field(default_factory=list)
    content: str | None = None
    status: int = 200
    finish: str | None = None


def call(name: str, args: Any, id: str | None = "call_m1") -> Call:
    """`args` as a dict is serialized; a str is sent exactly as written (malformed or not)."""
    return Call(name, json.dumps(args) if isinstance(args, dict) else args, id)


class _Chunked(httpx.AsyncByteStream):
    def __init__(self, data: bytes, size: int) -> None:
        self._data, self._size = data, size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self._data), self._size):
            yield self._data[i : i + self._size]


class FakeUpstream:
    """HandlerTransport handler that plays `replies` in order, one per chat call."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.sent: list[bytes] = []
        self.transport = HandlerTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not str(request.url).endswith("/chat/completions"):
            return httpx.Response(404, json={"error": "not found"})
        body = json.loads(request.content)
        self.requests.append(body)
        if len(self.requests) > len(self.replies):
            raise AssertionError("upstream called more often than the case scripts")
        reply = self.replies[len(self.requests) - 1]
        if reply.status != 200:
            self.sent.append(b"")
            return httpx.Response(reply.status, json={"error": {"message": "scripted failure"}})
        if body.get("stream"):
            data = _sse(reply, usage=bool((body.get("stream_options") or {}).get("include_usage")))
            self.sent.append(data)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_Chunked(data, 11),
            )
        data = json.dumps(_completion(reply)).encode()
        self.sent.append(data)
        return httpx.Response(200, content=data, headers={"content-type": "application/json"})


def _tool_calls(reply: Reply) -> list[dict[str, Any]]:
    out = []
    for c in reply.calls:
        item: dict[str, Any] = {
            "type": "function",
            "function": {"name": c.name, "arguments": c.arguments},
        }
        if c.id is not None:
            item["id"] = c.id
        out.append(item)
    return out


def _completion(reply: Reply) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.content}
    if reply.calls:
        message["tool_calls"] = _tool_calls(reply)
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": reply.finish or ("tool_calls" if reply.calls else "stop"),
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    }


def _sse(reply: Reply, *, usage: bool = False) -> bytes:
    """Chunks the way providers send them: role, name+id, then arguments in small pieces."""
    base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1, "model": MODEL}

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        body = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return b"data: " + json.dumps(body).encode() + b"\n\n"

    out = [chunk({"role": "assistant", "content": ""})]
    text = reply.content or ""
    out += [chunk({"content": text[i : i + 5]}) for i in range(0, len(text), 5)]
    for index, c in enumerate(reply.calls):
        first: dict[str, Any] = {"index": index, "type": "function"}
        if c.id is not None:
            first["id"] = c.id
        first["function"] = {"name": c.name, "arguments": ""}
        out.append(chunk({"tool_calls": [first]}))
        args = c.arguments if isinstance(c.arguments, str) else ""
        for i in range(0, len(args), 7):
            piece = {"index": index, "function": {"arguments": args[i : i + 7]}}
            out.append(chunk({"tool_calls": [piece]}))
    out.append(chunk({}, reply.finish or ("tool_calls" if reply.calls else "stop")))
    if usage:
        tail = {**base, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}
        out.append(b"data: " + json.dumps(tail).encode() + b"\n\n")
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


@dataclass
class Result:
    status: int
    body: bytes
    message: dict[str, Any] | None
    upstream: FakeUpstream
    error: dict[str, Any] | None = None


@contextmanager
def _env(**values: str | None) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def replay(
    request: dict[str, Any],
    replies: list[Reply],
    *,
    repair: bool,
    stream: bool,
    headers: dict[str, str],
) -> Result:
    upstream = FakeUpstream(replies)
    app = create_app(backend=OpenRouterBackend(transport=upstream.transport))
    body = {**request, "stream": stream}
    with _env(OS3_BRIDGE_TOOL_REPAIR=None if repair else "off"):
        with TestClient(app, base_url="http://127.0.0.1:11435") as client:
            response = client.post("/v1/chat/completions", json=body, headers=headers)
    error = error_of(response.content)
    message = None
    if response.status_code == 200 and error is None:
        message = read_stream(response.content) if stream else _first_message(response.content)
    return Result(response.status_code, response.content, message, upstream, error)


def error_of(raw: bytes) -> dict[str, Any] | None:
    """The `error` object of a JSON error body, or of an SSE error event."""
    for text in [raw.decode("utf-8", "replace"), *raw.decode("utf-8", "replace").split("\n\n")]:
        text = text.removeprefix("data: ")
        try:
            body = json.loads(text)
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return body["error"]
    return None


def _first_message(raw: bytes) -> dict[str, Any]:
    return json.loads(raw)["choices"][0]["message"]


def read_stream(raw: bytes) -> dict[str, Any]:
    """What an OpenAI SDK client builds from the stream. Fails on anything it would choke on."""
    text = raw.decode("utf-8")
    events = [e for e in re.split(r"\r?\n\r?\n", text) if e.strip()]
    assert events[-1].strip() == "data: [DONE]", "stream does not end with [DONE]"
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    for event in events[:-1]:
        assert event.startswith("data: "), f"not an SSE data event: {event[:40]!r}"
        obj = json.loads(event[len("data: ") :])
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(
                    tc["index"],
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                slot["id"] = tc.get("id") or slot["id"]
                slot["function"]["name"] += tc.get("function", {}).get("name") or ""
                slot["function"]["arguments"] += tc.get("function", {}).get("arguments") or ""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return message


def os3_rejects(request: dict[str, Any], message: dict[str, Any]) -> list[str]:
    """Why OS3 would fail this answer. Empty means OS3 runs it.

    Modelled on the executor os3-router's bench replays: strict JSON arguments, a
    known tool, the tool's own schema, a node_id that names a registered device,
    computer_use limited to three scripts, and a screenshot that is looked at.
    """
    tools = {t["function"]["name"]: t["function"].get("parameters") or {} for t in request["tools"]}
    system = " ".join(
        m["content"]
        for m in request["messages"]
        if m["role"] == "system" and isinstance(m["content"], str)
    )
    devices = set(re.findall(r'<node id="([^"]+)"', system))
    calls = message.get("tool_calls") or []
    if not calls:
        return [] if (message.get("content") or "").strip() else ["empty answer"]
    problems: list[str] = []
    seen: set[str] = set()
    parsed: list[tuple[str, dict[str, Any]]] = []
    for c in calls:
        fn = c.get("function") or {}
        name = fn.get("name")
        if not c.get("id") or c["id"] in seen:
            problems.append(f"{name}: missing or duplicate call id")
        seen.add(c.get("id") or "")
        if name not in tools:
            problems.append(f"unknown tool {name!r}")
            continue
        try:
            args = json.loads(fn.get("arguments"))
        except (TypeError, ValueError):
            problems.append(f"{name}: arguments are not valid JSON")
            continue
        if not isinstance(args, dict):
            problems.append(f"{name}: arguments are not an object")
            continue
        parsed.append((name, args))
        problems += [
            f"{name}: {e.message}" for e in Draft202012Validator(tools[name]).iter_errors(args)
        ]
        if (
            "node_id" in (tools[name].get("properties") or {})
            and args.get("node_id") not in devices
        ):
            problems.append(f"{name}: node_id is not a registered device")
        if name == "computer_use" and args.get("script") not in SCRIPTS:
            problems.append("computer_use: Bounded desktop script and literal arguments required")
    for i, (name, args) in enumerate(parsed):
        if name == "computer_use" and args.get("script") == "capture.py":
            later = [n for n, _ in parsed[i + 1 :]]
            if "feed_image" in tools and "feed_image" not in later:
                problems.append("computer_use capture.py is never looked at (no feed_image)")
    return problems


def calls_of(message: dict[str, Any]) -> list[tuple[str, Any]]:
    """(tool name, parsed arguments) per call; arguments that do not parse stay as text."""
    out = []
    for c in message.get("tool_calls") or []:
        raw = c["function"].get("arguments")
        try:
            out.append((c["function"]["name"], json.loads(raw)))
        except (TypeError, ValueError):
            out.append((c["function"]["name"], raw))
    return out


def verdict(request: dict[str, Any], expect: list[tuple[str, Any]], result: Result) -> str:
    if result.error is not None and result.error.get("code") == "invalid_tool_call":
        return "rejected (invalid_tool_call)"
    if result.status != 200 or result.message is None:
        return f"FAIL (HTTP {result.status})"
    problems = os3_rejects(request, result.message)
    if problems:
        return "FAIL: " + problems[0]
    if result.message.get("tool_calls") and calls_of(result.message) != expect:
        return "FAIL: valid but not the intended call"
    return "pass"


def table() -> str:
    from tests.os3_cases import CASES

    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "bridge.key"
        key.write_text("os3_test_bridge_key\n", encoding="utf-8")
        key.chmod(0o600)
        with _env(
            OS3_BRIDGE_KEY_FILE=str(key),
            OPENROUTER_API_KEY="sk-or-test-secret-value",
            OS3_BRIDGE_BACKEND="openrouter",
        ):
            headers = {"Authorization": "Bearer os3_test_bridge_key"}
            rows = [
                "| fixture | what the model sent | before | after |",
                "| --- | --- | --- | --- |",
            ]
            for case in CASES:
                request = load(case.fixture)
                cells = []
                for repair in (False, True):
                    seen = {
                        verdict(
                            request,
                            case.expect,
                            replay(request, case.replies, repair=repair, stream=s, headers=headers),
                        )
                        for s in (False, True)
                    }
                    cells.append(seen.pop() if len(seen) == 1 else "stream and non-stream differ")
                rows.append(f"| {case.fixture} | {case.summary} | {cells[0]} | {cells[1]} |")
    return "\n".join(rows)


if __name__ == "__main__":
    import logging

    logging.getLogger("nexus").setLevel(logging.CRITICAL)
    print(table())
