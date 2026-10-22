"""ChatGPT plan backend: translation, auth refresh, streaming and tools, no network."""

from __future__ import annotations

import base64
import json
import ssl
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import chatgpt_auth
from nexus.app import create_app
from nexus.backends import chatgpt as chatgpt_backend
from nexus.backends.chatgpt import ChatGPTBackend
from nexus.chatgpt_responses import ChatSseFromResponses, chat_to_responses
from nexus.errors import OUT_OF_QUOTA, GatewayError, classify_failure

from tests.transport import HandlerTransport

FIXTURES = Path(__file__).parent / "fixtures" / "os3"

ACCOUNT = "acct-test-123"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _jwt(exp: float, tag: str = "a") -> str:
    def enc(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': int(exp), 'tag': tag})}.sig-{tag}"


def _write_auth(path: Path, access: str, refresh: str = "rt-old") -> None:
    data = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "idt",
            "access_token": access,
            "refresh_token": refresh,
            "account_id": ACCOUNT,
        },
        "last_refresh": "2026-10-01T00:00:00Z",
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def auth_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "codex-auth.json"
    _write_auth(path, _jwt(time.time() + 3600))
    monkeypatch.setenv("NEXUS_CHATGPT_AUTH_FILE", str(path))
    monkeypatch.setenv("NEXUS_BACKEND", "chatgpt")
    chatgpt_backend._models_cache.clear()
    return path


def _sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def _tool_call_events(name: str, args: str, call_id: str = "call_x1") -> list[dict[str, Any]]:
    item = {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name}
    half = len(args) // 2
    return [
        {"type": "response.created", "response": {"model": "gpt-5.6-sol"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": args[:half]},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": args[half:]},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {**item, "arguments": args},
        },
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-5.6-sol",
                "status": "completed",
                "output": [{**item, "arguments": args}],
                "usage": {"input_tokens": 40, "output_tokens": 9},
            },
        },
    ]


def _text_events(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"model": "gpt-5.6-sol"}},
        {"type": "response.output_text.delta", "delta": text[:3]},
        {"type": "response.output_text.delta", "delta": text[3:]},
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-5.6-sol",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        },
    ]


class Upstream:
    def __init__(self, replies: list[tuple[int, bytes]]) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.transport = HandlerTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-6-sol", "visibility": "list"},
                        {"slug": "gpt-6-luna", "visibility": "list"},
                        {"slug": "gpt-5.5", "visibility": "hide"},
                    ]
                },
            )
        assert request.url.path.endswith("/responses")
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        status, data = self.replies.pop(0)
        content_type = "text/event-stream" if status == 200 else "application/json"
        return httpx.Response(status, content=data, headers={"content-type": content_type})


def _client(upstream: Upstream) -> TestClient:
    return TestClient(create_app(backend=ChatGPTBackend(transport=upstream.transport)))


def test_worker_fixture_translates_to_responses() -> None:
    request = load("worker.json")
    body = chat_to_responses(request, model="gpt-5.6-sol")
    assert body["stream"] is True and body["store"] is False
    assert "worker agent" in body["instructions"]
    kinds = [item["type"] for item in body["input"]]
    assert kinds[0] == "message"
    assert kinds.count("function_call") == kinds.count("function_call_output") == 2
    calls = [i for i in body["input"] if i["type"] == "function_call"]
    outputs = [i for i in body["input"] if i["type"] == "function_call_output"]
    assert [c["call_id"] for c in calls] == [o["call_id"] for o in outputs]
    assert all(isinstance(c["arguments"], str) for c in calls)
    assert {t["name"] for t in body["tools"]} == {t["function"]["name"] for t in request["tools"]}
    assert all(t["type"] == "function" and "parameters" in t for t in body["tools"])
    assert body["reasoning"] == {"effort": request["reasoning_effort"]}
    assert all(m.get("role") != "system" for m in body["input"])


def test_computer_use_screenshot_becomes_input_image() -> None:
    body = chat_to_responses(load("computer_use.json"), model="gpt-5.6-sol")
    images = [
        part
        for item in body["input"]
        if item["type"] == "message"
        for part in item["content"]
        if part["type"] == "input_image"
    ]
    assert len(images) == 1
    assert images[0]["image_url"].startswith("data:image/")


def test_named_tool_choice_and_no_tools() -> None:
    body = chat_to_responses(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}},
        },
        model="m",
    )
    assert body["tool_choice"] == {"type": "function", "name": "f"}
    plain = chat_to_responses({"messages": [{"role": "user", "content": "hi"}]}, model="m")
    assert "tools" not in plain and plain["instructions"]


def test_sse_translator_handles_arguments_only_in_done_event() -> None:
    translator = ChatSseFromResponses("m")
    item = {"type": "function_call", "id": "fc_9", "call_id": "c9", "name": "shell"}
    events = [
        {"type": "response.output_item.added", "item": {**item, "arguments": ""}},
        {"type": "response.output_item.done", "item": {**item, "arguments": '{"cmd":"ls"}'}},
        {"type": "response.completed", "response": {"output": []}},
    ]
    chunks = [json.loads(c[6:]) for c in translator.feed(_sse(events))]
    args = "".join(
        tc["function"].get("arguments", "")
        for c in chunks
        for tc in c["choices"][0]["delta"].get("tool_calls", [])
    )
    assert args == '{"cmd":"ls"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("fixture", ["chat.json", "worker.json", "computer_use.json"])
def test_os3_fixture_tool_call_round_trip(
    fixture: str, stream: bool, auth_file: Path, auth_header: dict[str, str]
) -> None:
    upstream = Upstream(
        [(200, _sse(_tool_call_events("notify_before_act", '{"message":"Checking now."}')))]
    )
    request = {**load(fixture), "stream": stream}
    with _client(upstream) as client:
        response = client.post("/v1/chat/completions", json=request, headers=auth_header)
    assert response.status_code == 200
    sent = upstream.headers[0]
    token = json.loads(auth_file.read_text())["tokens"]["access_token"]
    assert sent["authorization"] == f"Bearer {token}"
    assert sent["chatgpt-account-id"] == ACCOUNT
    assert upstream.bodies[0]["tools"]
    assert token not in response.text and "rt-old" not in response.text
    if stream:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.rstrip().endswith("data: [DONE]")
        chunks = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
        assert calls[0]["function"]["name"] == "notify_before_act" and calls[0]["id"] == "call_x1"
        args = "".join(tc["function"].get("arguments", "") for tc in calls)
        assert json.loads(args) == {"message": "Checking now."}
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    else:
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        call = choice["message"]["tool_calls"][0]
        assert call["id"] == "call_x1" and call["function"]["name"] == "notify_before_act"
        assert json.loads(call["function"]["arguments"]) == {"message": "Checking now."}
        assert response.json()["usage"]["prompt_tokens"] == 40


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_plain_text_answer(stream: bool, auth_file: Path, auth_header: dict[str, str]) -> None:
    upstream = Upstream([(200, _sse(_text_events("hello there")))])
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": stream}
    with _client(upstream) as client:
        response = client.post("/v1/chat/completions", json=body, headers=auth_header)
    assert response.status_code == 200
    if stream:
        text = "".join(
            json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            for line in response.text.splitlines()
            if line.startswith("data: {")
        )
        assert text == "hello there"
    else:
        assert response.json()["choices"][0]["message"]["content"] == "hello there"
    assert upstream.bodies[0]["model"] == "gpt-6-luna"


def test_models_come_from_the_plan(auth_file: Path, auth_header: dict[str, str]) -> None:
    upstream = Upstream([])
    with _client(upstream) as client:
        models = client.get("/v1/models", headers=auth_header).json()["data"]
        health = client.get("/health").json()
    assert [m["id"] for m in models] == ["gpt-6-luna", "gpt-6-sol", "gpt-5.5", "gpt-image-2"]
    assert models[-1]["kind"] == "image"
    row = health["backends"]["chatgpt"]
    assert row["state"] == "ready" and row["login"] == "ok"
    assert health["ready"] is True
    assert "access_token" not in json.dumps(health)


def test_missing_login_is_not_ready_and_not_activatable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CHATGPT_AUTH_FILE", str(tmp_path / "nope.json"))
    row = ChatGPTBackend().describe()
    assert row["ready"] is False and row["login"] == "missing"
    assert "codex login" in row["hint"]
    from nexus.setup_flow import SetupError, apply_dashboard_choice

    with pytest.raises(SetupError) as raised:
        apply_dashboard_choice(backend="chatgpt", api_key=None, model=None)
    assert raised.value.code == "not_ready"


def test_api_key_login_is_unusable(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"OPENAI_API_KEY": "sk-x", "tokens": None}))
    monkeypatch.setenv("NEXUS_CHATGPT_AUTH_FILE", str(path))
    status, hint = chatgpt_auth.login_status()
    assert status == "unusable" and "sk-x" not in hint


def test_expiring_token_is_refreshed_in_place(
    auth_file: Path, auth_header: dict[str, str], monkeypatch
) -> None:
    _write_auth(auth_file, _jwt(time.time() + 30, "old"))
    new = _jwt(time.time() + 3600, "new")
    sent: list[str] = []

    def fake_refresh(refresh: str) -> dict[str, Any]:
        sent.append(refresh)
        return {"access_token": new, "refresh_token": "rt-new", "id_token": "idt2"}

    monkeypatch.setattr(chatgpt_auth, "_post_refresh", fake_refresh)
    upstream = Upstream([(200, _sse(_text_events("ok!"))), (200, _sse(_text_events("ok!")))])
    body = {"messages": [{"role": "user", "content": "hi"}]}
    with _client(upstream) as client:
        assert (
            client.post("/v1/chat/completions", json=body, headers=auth_header).status_code == 200
        )
        assert (
            client.post("/v1/chat/completions", json=body, headers=auth_header).status_code == 200
        )
    assert sent == ["rt-old"]
    assert [h["authorization"] for h in upstream.headers] == [f"Bearer {new}"] * 2
    saved = json.loads(auth_file.read_text())
    assert saved["tokens"]["access_token"] == new
    assert saved["tokens"]["refresh_token"] == "rt-new"
    assert saved["tokens"]["account_id"] == ACCOUNT
    assert saved["auth_mode"] == "chatgpt" and saved["last_refresh"] != "2026-10-01T00:00:00Z"
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_a_401_forces_one_refresh_and_retries(
    auth_file: Path, auth_header: dict[str, str], monkeypatch
) -> None:
    new = _jwt(time.time() + 7200, "new")
    monkeypatch.setattr(chatgpt_auth, "_post_refresh", lambda refresh: {"access_token": new})
    upstream = Upstream([(401, b'{"detail":"expired"}'), (200, _sse(_text_events("again")))])
    body = {"messages": [{"role": "user", "content": "hi"}]}
    with _client(upstream) as client:
        response = client.post("/v1/chat/completions", json=body, headers=auth_header)
    assert response.status_code == 200
    assert upstream.headers[1]["authorization"] == f"Bearer {new}"


def test_refresh_rejected_says_log_in_again(auth_file: Path, monkeypatch) -> None:
    _write_auth(auth_file, _jwt(time.time() - 10))

    def refused(refresh: str) -> dict[str, Any]:
        raise chatgpt_auth._not_logged_in("expired")

    monkeypatch.setattr(chatgpt_auth, "_post_refresh", refused)
    import asyncio

    with pytest.raises(GatewayError) as raised:
        asyncio.run(chatgpt_auth.access())
    assert raised.value.code == "chatgpt_not_logged_in"
    assert "codex login" in raised.value.extra["hint"]


def test_usage_limit_is_a_quota_failure(auth_file: Path, auth_header: dict[str, str]) -> None:
    limit = json.dumps(
        {"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached"}}
    ).encode()
    upstream = Upstream([(429, limit)])
    body = {"messages": [{"role": "user", "content": "hi"}]}
    with _client(upstream) as client:
        response = client.post("/v1/chat/completions", json=body, headers=auth_header)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "chatgpt_quota"
    exc = GatewayError(
        429,
        "x",
        "rate_limit_error",
        "chatgpt_quota",
        extra={
            "upstream": {
                "backend": "chatgpt",
                "upstream_status": 429,
                "upstream_body": limit.decode(),
            }
        },
    )
    assert classify_failure(exc) == OUT_OF_QUOTA


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_tls_error_is_a_clean_upstream_error(
    stream: bool, auth_file: Path, auth_header: dict[str, str]
) -> None:
    # Seen live: ssl.SSLError [SSL: SSLV3_ALERT_BAD_RECORD_MAC] escaped httpx and the door
    # answered a bare HTTP 500.
    def handle(request: httpx.Request) -> httpx.Response:
        raise ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac")

    app = create_app(backend=ChatGPTBackend(transport=HandlerTransport(handle)))
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": stream}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/chat/completions", json=body, headers=auth_header)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_ssl_error"


class _CutStream(httpx.AsyncByteStream):
    """Sends the first bytes after the 200, then dies with the TLS error seen live."""

    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self):  # type: ignore[override]
        yield self.first
        raise ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac")


def test_tls_error_mid_stream_ends_the_stream_cleanly(
    auth_file: Path, auth_header: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    first = _sse(_text_events("hello there")[:2])

    def handle(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=headers, stream=_CutStream(first))

    app = create_app(backend=ChatGPTBackend(transport=HandlerTransport(handle)))
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/chat/completions", json=body, headers=auth_header)
    assert response.status_code == 200
    assert '"code": "upstream_ssl_error"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert response.text.count("[DONE]") == 1
    assert "upstream SSL error mid-stream backend=chatgpt" in caplog.text
    assert "unhandled" not in caplog.text


def test_failed_stream_ends_with_an_error_event(
    auth_file: Path, auth_header: dict[str, str]
) -> None:
    events = [
        {"type": "response.created", "response": {}},
        {
            "type": "response.failed",
            "response": {"error": {"code": "server_error", "message": "boom"}},
        },
    ]
    upstream = Upstream([(200, _sse(events))])
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    with _client(upstream) as client:
        response = client.post("/v1/chat/completions", json=body, headers=auth_header)
    assert '"code": "chatgpt_response_failed"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_empty_final_output_keeps_streamed_tool_call_and_text() -> None:
    from nexus.chatgpt_responses import completion_from_events

    item = {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "skill_view"}
    events = [
        {"type": "response.output_item.added", "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{}"},
        {"type": "response.output_item.done", "item": {**item, "arguments": '{"name":"dlam"}'}},
        {"type": "response.completed", "response": {"status": "completed", "output": []}},
    ]
    choice = completion_from_events(events, model="m")["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "c1" and call["function"]["arguments"] == '{"name":"dlam"}'

    text_events = [
        {"type": "response.output_text.delta", "delta": "hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.completed", "response": {"status": "completed", "output": []}},
    ]
    message = completion_from_events(text_events, model="m")["choices"][0]["message"]
    assert message["content"] == "hello"


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_empty_final_output_through_the_door(
    stream: bool, auth_file: Path, auth_header: dict[str, str]
) -> None:
    events = _tool_call_events("notify_before_act", '{"message":"Checking now."}')
    events[-1]["response"]["output"] = []
    upstream = Upstream([(200, _sse(events))])
    request = {**load("computer_use.json"), "stream": stream}
    with _client(upstream) as client:
        response = client.post("/v1/chat/completions", json=request, headers=auth_header)
    assert response.status_code == 200
    assert "notify_before_act" in response.text and "tool_calls" in response.text
