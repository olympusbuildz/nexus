"""./nexus test, against the real ASGI app with a fake upstream. No sockets."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import selftest
from nexus.app import create_app
from nexus.backends.base import ChatUpstream
from nexus.backends.openrouter import OpenRouterBackend
from nexus.cli import main
from nexus.config import DEFAULT_MODEL

from tests.transport import HandlerTransport, key_ok

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "nexus_add", "arguments": json.dumps({"a": 2, "b": 3})},
}


def _completion(message: dict[str, Any], finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": DEFAULT_MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        },
    )


def _upstream(tool_reply: dict[str, Any] | None = None, tool_status: int = 200):
    """A well-behaved upstream unless told how the tool turn should go."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": DEFAULT_MODEL, "object": "model"}]})
        body = json.loads(request.content.decode())
        seen.append(body)
        if "tools" not in body:
            return _completion({"role": "assistant", "content": "ok"})
        if tool_status != 200:
            return httpx.Response(tool_status, json={"error": {"message": "no tool use"}})
        if any(m.get("role") == "tool" for m in body["messages"]):
            return _completion({"role": "assistant", "content": "2 + 3 = 5"})
        message = tool_reply or {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]}
        return _completion(message, "tool_calls" if message.get("tool_calls") else "stop")

    return key_ok(handler), seen


def _run(app, out: list[str]) -> int:
    transport = httpx.ASGITransport(app=app)
    return asyncio.run(selftest.run_selftest(transport=transport, out=out.append))


def _openrouter_app(handler):
    return create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))


def _text(out: list[str]) -> str:
    return "\n".join(out)


def test_pass_prints_settings_and_never_the_key(bridge_key: str, openrouter_key: str) -> None:
    handler, seen = _upstream()
    out: list[str] = []
    code = _run(_openrouter_app(handler), out)
    text = _text(out)
    assert code == 0, text
    assert "Base URL   http://127.0.0.1:11435/v1" in text
    assert "if your client returns 403 for 127.0.0.1, try http://127.1:11435/v1" in text
    assert "Backend    openrouter" in text
    assert f"Model      {DEFAULT_MODEL}" in text
    assert "the one line in " in text
    assert bridge_key not in text
    assert openrouter_key not in text
    for name in ("server", "backend", "/v1 via 127.0.0.1", "/v1 via 127.1", "chat", "tool call"):
        assert name in text
    assert "FAIL" not in text.replace("RESULT: PASS", "")
    assert "RESULT: PASS (5 passed, 0 skipped)" in text
    tool_turns = [b for b in seen if "tools" in b]
    assert len(tool_turns) == 2
    assert tool_turns[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "5",
    }


def test_reports_which_loopback_names_answered(bridge_key: str, openrouter_key: str) -> None:
    handler, _ = _upstream()
    out: list[str] = []
    _run(_openrouter_app(handler), out)
    for host in ("127.0.0.1", "127.1"):
        assert any(f"/v1 via {host} " in line and "answered 200" in line for line in out), out


def test_v1_accepts_host_127_1_and_dashboard_allowlist_is_unchanged(bridge_key: str) -> None:
    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(key_ok(_upstream()[0]))))
    client = TestClient(app, base_url="http://127.1:11435")
    models = client.get("/v1/models", headers={"Authorization": f"Bearer {bridge_key}"})
    assert models.status_code == 200
    assert client.get("/health").status_code == 403


def test_tool_step_is_skipped_for_a_subscription_cli(
    bridge_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCli:
        id = "codex"

        def describe(self) -> dict[str, Any]:
            return {"ready": True, "implemented": True}

        async def model_reachable(self, model: str) -> bool:
            return True

        async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
            if payload.get("tools"):
                raise AssertionError("the test must not send tools to a subscription CLI")
            body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
            return ChatUpstream(200, "application/json", body=json.dumps(body).encode())

        async def list_models(self) -> list[dict[str, Any]]:
            return [{"id": "gpt-5.6-sol", "object": "model"}]

    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    out: list[str] = []
    code = _run(create_app(backend=FakeCli()), out)
    text = _text(out)
    assert code == 0, text
    assert "SKIP  tool call" in text
    assert "subscription CLIs refuse tools" in text
    assert "RESULT: PASS (4 passed, 1 skipped)" in text


def test_tool_step_runs_on_the_chatgpt_plan(
    bridge_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeChatGPT:
        id = "chatgpt"

        def describe(self) -> dict[str, Any]:
            return {"ready": True, "implemented": True}

        async def model_reachable(self, model: str) -> bool:
            return True

        async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
            message: dict[str, Any] = {"role": "assistant", "content": "ok"}
            if any(m.get("role") == "tool" for m in payload["messages"]):
                message = {"role": "assistant", "content": "2 + 3 = 5"}
            elif payload.get("tools"):
                message = {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]}
            body = {"choices": [{"index": 0, "message": message}]}
            return ChatUpstream(200, "application/json", body=json.dumps(body).encode())

        async def list_models(self) -> list[dict[str, Any]]:
            return [{"id": "gpt-6-luna", "object": "model"}]

    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "chatgpt")
    out: list[str] = []
    code = _run(create_app(backend=FakeChatGPT()), out)
    text = _text(out)
    assert code == 0, text
    assert "PASS  tool call" in text
    assert "RESULT: PASS (5 passed, 0 skipped)" in text


def test_antigravity_gate_off_skips_chat_and_tools_and_still_exits_zero(
    bridge_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus.backends.subscription import AntigravityBackend

    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "antigravity")
    monkeypatch.delenv("OS3_BRIDGE_ENABLE_ANTIGRAVITY", raising=False)
    out: list[str] = []
    code = _run(create_app(backend=AntigravityBackend()), out)
    text = _text(out)
    assert code == 0, text
    assert "PASS  server" in text
    assert "FAIL" not in text.replace("RESULT: PASS", "")
    for name in ("backend", "chat", "tool call"):
        assert f"SKIP  {name}" in text
    assert text.count("NEXUS_ENABLE_ANTIGRAVITY=1") >= 3
    assert "RESULT: PASS (2 passed, 3 skipped)" in text


def test_antigravity_gate_on_runs_the_normal_steps(
    bridge_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeAgy:
        id = "antigravity"

        def describe(self) -> dict[str, Any]:
            return {"ready": True, "implemented": True, "enabled": True}

        async def model_reachable(self, model: str) -> bool:
            return True

        async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
            body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
            return ChatUpstream(200, "application/json", body=json.dumps(body).encode())

        async def list_models(self) -> list[dict[str, Any]]:
            return [{"id": "gemini-3.8-flash-high", "object": "model"}]

    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "antigravity")
    monkeypatch.setenv("OS3_BRIDGE_ENABLE_ANTIGRAVITY", "1")
    out: list[str] = []
    code = _run(create_app(backend=FakeAgy()), out)
    text = _text(out)
    assert code == 0, text
    assert "PASS  backend" in text
    assert "PASS  chat" in text
    assert "SKIP  tool call" in text
    assert "subscription CLIs refuse tools" in text
    assert "SKIP  backend" not in text
    assert "SKIP  chat" not in text
    assert "RESULT: PASS (4 passed, 1 skipped)" in text


def test_fail_when_the_model_ignores_tools(bridge_key: str, openrouter_key: str) -> None:
    handler, _ = _upstream({"role": "assistant", "content": "It is 5."})
    out: list[str] = []
    code = _run(_openrouter_app(handler), out)
    text = _text(out)
    assert code == 1
    assert "FAIL  tool call" in text
    assert "called no tool" in text
    assert "fix: This model did not do tools" in text
    assert "PASS  chat" in text


def test_fail_when_the_tool_call_breaks_the_schema(bridge_key: str, openrouter_key: str) -> None:
    bad = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "nexus_add", "arguments": '{"a": "two", "b": 3}'},
            }
        ],
    }
    handler, _ = _upstream(bad)
    out: list[str] = []
    assert _run(_openrouter_app(handler), out) == 1
    assert "FAIL  tool call" in _text(out)


def test_fail_when_upstream_refuses_tools_outright(bridge_key: str, openrouter_key: str) -> None:
    handler, _ = _upstream(tool_status=404)
    out: list[str] = []
    assert _run(_openrouter_app(handler), out) == 1
    assert "FAIL  tool call" in _text(out)
    assert "fix: This model did not do tools" in _text(out)


def test_fail_when_the_backend_has_no_key(bridge_key: str) -> None:
    handler, _ = _upstream()
    out: list[str] = []
    code = _run(_openrouter_app(handler), out)
    text = _text(out)
    assert code == 1
    assert "FAIL  backend" in text
    assert "./nexus setup" in text
    assert "chat" not in text.split("Checks:")[1]


def test_fail_when_the_bridge_key_is_refused(
    bridge_key: str, openrouter_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selftest, "read_bridge_key", lambda: "os3_not_the_servers_key")
    handler, _ = _upstream()
    out: list[str] = []
    code = _run(_openrouter_app(handler), out)
    text = _text(out)
    assert code == 1
    assert "FAIL  /v1 via 127.0.0.1" in text
    assert "HTTP 401" in text
    assert "The bridge key was refused" in text
    assert "os3_not_the_servers_key" not in text


def test_fail_when_the_server_is_down() -> None:
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    out: list[str] = []
    code = asyncio.run(selftest.run_selftest(transport=Down(), out=out.append))
    text = _text(out)
    assert code == 1
    assert "FAIL  server" in text
    assert "nothing is listening on 127.0.0.1:11435" in text
    assert "fix: Start it: ./nexus start" in text
    assert "Base URL   http://127.0.0.1:11435/v1" in text


def test_fail_names_the_holder_when_the_server_is_not_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stranger(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"hello": "world"})

    monkeypatch.setattr(
        "nexus.port_holder.report",
        lambda port: [
            f"LISTEN 127.0.0.1:{port} (process left running):",
            "  pid 4242 node server.js",
            "    held by: not nexus",
        ],
    )
    out: list[str] = []
    code = asyncio.run(selftest.run_selftest(transport=Stranger(), out=out.append))
    text = _text(out)
    assert code == 1
    assert "answered, but not as nexus" in text
    assert "pid 4242 node server.js" in text
    assert "held by: not nexus" in text


def test_cli_test_command_exits_with_the_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake(**_kw: Any) -> int:
        print("RESULT: FAIL")
        return 1

    monkeypatch.setattr("nexus.selftest.run_selftest", fake)
    assert main(["test"]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_tool_arguments_validation() -> None:
    assert selftest.tool_arguments_error('{"a": 1, "b": 2}') is None
    assert selftest.tool_arguments_error({"a": 1, "b": 2}) is None
    assert selftest.tool_arguments_error("nope")
    assert selftest.tool_arguments_error("[1]")
    assert selftest.tool_arguments_error('{"a": 1}')
    assert selftest.tool_arguments_error('{"a": 1, "b": 2, "c": 3}')
    assert selftest.tool_arguments_error('{"a": true, "b": 2}')
    assert selftest.tool_arguments_error('{"a": 1.5, "b": 2}')


def test_wait_rides_out_a_door_that_is_still_binding(
    bridge_key: str, openrouter_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, _ = _upstream()
    inner = httpx.ASGITransport(app=_openrouter_app(handler))
    refusals = {"left": 2}

    class Late(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health" and refusals["left"] > 0:
                refusals["left"] -= 1
                raise httpx.ConnectError("refused", request=request)
            return await inner.handle_async_request(request)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(selftest.asyncio, "sleep", no_sleep)
    out: list[str] = []
    assert asyncio.run(selftest.run_selftest(transport=Late(), out=out.append, wait=30)) == 0
    refusals["left"] = 2
    out = []
    assert asyncio.run(selftest.run_selftest(transport=Late(), out=out.append, wait=0)) == 1
    assert "nothing is listening" in _text(out)
