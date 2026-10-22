"""ChatGPT backend: up to two retries when opening the upstream dies in TLS, no network."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends import chatgpt as chatgpt_backend
from nexus.backends.chatgpt import ChatGPTBackend

from tests.test_chatgpt import _sse, _text_events, auth_file  # noqa: F401
from tests.transport import HandlerTransport, sent_to

BAD_MAC = "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chatgpt_backend, "_OPEN_RETRY_BACKOFF_S", ((0.0, 0.0), (0.0, 0.0)))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(_text_events("hello there")),
        headers={"content-type": "text/event-stream"},
    )


def _ssl_error() -> Exception:
    return ssl.SSLError(1, BAD_MAC)


def _door(
    handle: Callable[[httpx.Request], httpx.Response],
) -> tuple[TestClient, HandlerTransport]:
    transport = HandlerTransport(handle)
    app = create_app(backend=ChatGPTBackend(transport=transport))
    return TestClient(app, raise_server_exceptions=False), transport


def _flaky(failures: int, error: Callable[[], Exception] = _ssl_error):
    state = {"left": failures}

    def handle(request: httpx.Request) -> httpx.Response:
        if state["left"]:
            state["left"] -= 1
            raise error()
        return _ok(request)

    return handle


def _chat(client: TestClient, headers: dict[str, str], *, stream: bool) -> httpx.Response:
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": stream}
    with client:
        return client.post("/v1/chat/completions", json=body, headers=headers)


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_first_ssl_error_then_success_gives_a_normal_reply(
    stream: bool,
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="nexus")
    client, transport = _door(_flaky(1))
    response = _chat(client, auth_header, stream=stream)
    assert response.status_code == 200
    assert len(sent_to(transport, "/responses")) == 2
    if stream:
        assert "hello there"[:3] in response.text
        assert response.text.rstrip().endswith("data: [DONE]")
        assert '"error"' not in response.text
    else:
        assert response.json()["choices"][0]["message"]["content"] == "hello there"
    assert "retry 1 of 2" in caplog.text
    assert "retry recovered" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        lambda: httpx.ConnectError("connection refused"),
        lambda: httpx.ConnectTimeout("connect timed out"),
        lambda: httpx.ReadError("connection reset by peer"),
    ],
    ids=["refused", "connect-timeout", "reset"],
)
def test_other_connection_level_errors_are_retried_too(
    error: Callable[[], Exception],
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
) -> None:
    client, transport = _door(_flaky(1, error))
    response = _chat(client, auth_header, stream=False)
    assert response.status_code == 200
    assert len(sent_to(transport, "/responses")) == 2


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_all_attempts_failing_is_the_same_502_as_before(
    stream: bool,
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, transport = _door(_flaky(5))
    response = _chat(client, auth_header, stream=stream)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_ssl_error"
    assert error["message"] == "upstream SSL error"
    assert len(sent_to(transport, "/responses")) == 3
    assert "did not recover after 2 retries" in caplog.text


def test_all_attempts_failing_keeps_the_unreachable_shape(
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
) -> None:
    client, transport = _door(_flaky(5, lambda: httpx.ConnectError("refused")))
    response = _chat(client, auth_header, stream=False)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "chatgpt_unreachable"
    assert len(sent_to(transport, "/responses")) == 3


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_second_failure_is_retried_again_and_recovers(
    stream: bool,
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Seen live 2026-10-19: SSLError, then ReadError 0.5s later, then a 502.
    caplog.set_level("INFO", logger="nexus")
    errors = iter([_ssl_error, lambda: httpx.ReadError("connection reset by peer")])

    def handle(request: httpx.Request) -> httpx.Response:
        error = next(errors, None)
        if error is not None:
            raise error()
        return _ok(request)

    client, transport = _door(handle)
    response = _chat(client, auth_header, stream=stream)
    assert response.status_code == 200
    assert len(sent_to(transport, "/responses")) == 3
    assert "retry 2 of 2" in caplog.text
    assert "recovered after 2 retries" in caplog.text


def test_second_retry_waits_longer_than_the_first(
    monkeypatch: pytest.MonkeyPatch,
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
) -> None:
    monkeypatch.setattr(chatgpt_backend, "_OPEN_RETRY_BACKOFF_S", ((0.3, 0.5), (1.0, 1.5)))
    pauses: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(chatgpt_backend.asyncio, "sleep", fake_sleep)
    client, transport = _door(_flaky(5))
    response = _chat(client, auth_header, stream=False)
    assert response.status_code == 502
    assert len(pauses) == 2
    assert 0.3 <= pauses[0] <= 0.5
    assert 1.0 <= pauses[1] <= 1.5


@pytest.mark.parametrize("status", [400, 403, 429, 500, 502, 503])
def test_an_http_status_error_is_not_retried(
    status: int,
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    client, transport = _door(handle)
    response = _chat(client, auth_header, stream=False)
    assert response.status_code != 200
    assert len(sent_to(transport, "/responses")) == 1


def test_a_read_timeout_is_not_retried(
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
) -> None:
    client, transport = _door(_flaky(5, lambda: httpx.ReadTimeout("slow")))
    response = _chat(client, auth_header, stream=False)
    assert response.status_code == 504
    assert len(sent_to(transport, "/responses")) == 1


class _CutStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self):  # type: ignore[override]
        yield self.first
        raise ssl.SSLError(1, BAD_MAC)


def test_no_retry_once_streaming_has_begun(
    auth_file: Path,  # noqa: F811
    auth_header: dict[str, str],
) -> None:
    first = _sse(_text_events("hello there")[:2])

    def handle(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=headers, stream=_CutStream(first))

    client, transport = _door(handle)
    response = _chat(client, auth_header, stream=True)
    assert response.status_code == 200
    assert '"code": "upstream_ssl_error"' in response.text
    assert response.text.count("[DONE]") == 1
    assert len(sent_to(transport, "/responses")) == 1
