"""An id the active backend does not serve is mapped to its configured model (real app)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends.nous import NousBackend

from tests.transport import HandlerTransport

ACTIVE = "meituan/longcat-2.5-preview:free"
LISTED = ["meituan/longcat-2.5-preview:free", "Vendor/Mixed-Case:free", "other/model"]
UNKNOWN = "stealth/space-bunny-alpha"
HEADER = "x-os3-model-remapped"


@pytest.fixture(autouse=True)
def nous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OS3_BRIDGE_MODEL_FALLBACK")
    monkeypatch.setenv("NOUS_API_KEY", "nous-test-secret-value")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    monkeypatch.setenv("OS3_BRIDGE_MODEL", ACTIVE)


def _app(listed: list[str] | None = LISTED) -> tuple[TestClient, HandlerTransport]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if listed is None:
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, json={"data": [{"id": name} for name in listed]})
        sent = json.loads(request.content)
        if sent.get("stream"):
            chunk = {"model": sent["model"], "choices": [{"index": 0, "delta": {"content": "hi"}}]}
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
            )
        return httpx.Response(
            200,
            json={
                "model": sent["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            },
        )

    transport = HandlerTransport(handler)
    return TestClient(create_app(backend=NousBackend(transport=transport))), transport


def _chat(client: TestClient, headers: dict[str, str], model: str | None, *, stream: bool = False):
    body: dict = {"messages": [{"role": "user", "content": "hi"}], "stream": stream}
    if model is not None:
        body["model"] = model
    return client.post("/v1/chat/completions", headers=headers, json=body)


def _upstream_models(transport: HandlerTransport) -> list[str]:
    return [json.loads(r.content)["model"] for r in transport.requests if r.method == "POST"]


def _stream_model(response: httpx.Response) -> str:
    line = next(ln for ln in response.text.splitlines() if ln.startswith("data: {"))
    return json.loads(line[6:])["model"]


def test_unknown_id_is_served_on_the_active_model(
    auth_header: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    client, transport = _app()
    with caplog.at_level(logging.INFO, logger="nexus"), client:
        response = _chat(client, auth_header, UNKNOWN)
        pulse = client.get("/dashboard/pulse").json()
    assert response.status_code == 200
    assert response.json()["model"] == ACTIVE
    assert response.headers[HEADER] == UNKNOWN
    assert _upstream_models(transport) == [ACTIVE]
    assert pulse["traffic"]["model_remapped"] == 1
    lines = [r.getMessage() for r in caplog.records if "remapped" in r.getMessage()]
    assert lines == [f"model remapped backend=nous {UNKNOWN} -> {ACTIVE}"]
    assert "nous-test-secret-value" not in caplog.text


def test_unknown_id_on_a_stream_is_served_on_the_active_model(auth_header: dict[str, str]) -> None:
    client, transport = _app()
    with client:
        response = _chat(client, auth_header, UNKNOWN, stream=True)
    assert response.status_code == 200
    assert _stream_model(response) == ACTIVE
    assert response.headers[HEADER] == UNKNOWN
    assert response.text.rstrip().endswith("data: [DONE]")
    assert _upstream_models(transport) == [ACTIVE]


@pytest.mark.parametrize("stream", [False, True])
def test_a_listed_id_is_untouched(auth_header: dict[str, str], stream: bool) -> None:
    client, transport = _app()
    with client:
        response = _chat(client, auth_header, "other/model", stream=stream)
    assert HEADER not in response.headers
    assert _upstream_models(transport) == ["other/model"]
    with client:
        assert client.get("/dashboard/pulse").json()["traffic"]["model_remapped"] == 0


def test_no_model_takes_the_existing_default_path(auth_header: dict[str, str]) -> None:
    client, transport = _app()
    with client:
        response = _chat(client, auth_header, None)
    assert HEADER not in response.headers
    assert _upstream_models(transport) == [ACTIVE]


@pytest.mark.parametrize("listed", [None, []])
def test_an_unavailable_list_passes_the_id_through(
    auth_header: dict[str, str], listed: list[str] | None
) -> None:
    client, transport = _app(listed)
    with client:
        response = _chat(client, auth_header, UNKNOWN)
    assert HEADER not in response.headers
    assert response.json()["model"] == UNKNOWN
    assert _upstream_models(transport) == [UNKNOWN]


def test_off_keeps_todays_behavior(
    auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_FALLBACK", "off")
    client, transport = _app()
    with client:
        unknown = _chat(client, auth_header, UNKNOWN)
        cased = _chat(client, auth_header, " vendor/mixed-case:free ")
        streamed = _chat(client, auth_header, UNKNOWN, stream=True)
    assert [r for r in transport.requests if r.method == "GET"] == []
    assert _upstream_models(transport) == [UNKNOWN, " vendor/mixed-case:free ", UNKNOWN]
    assert unknown.json()["model"] == UNKNOWN
    assert cased.json()["model"] == " vendor/mixed-case:free "
    assert _stream_model(streamed) == UNKNOWN
    assert HEADER not in unknown.headers
    assert HEADER not in streamed.headers


@pytest.mark.parametrize("sent", ["vendor/mixed-case:free", "  VENDOR/MIXED-CASE:FREE\t"])
def test_a_case_or_whitespace_variant_is_canonicalized_not_remapped(
    auth_header: dict[str, str], sent: str
) -> None:
    client, transport = _app()
    with client:
        response = _chat(client, auth_header, sent)
        pulse = client.get("/dashboard/pulse").json()
    assert response.json()["model"] == "Vendor/Mixed-Case:free"
    assert HEADER not in response.headers
    assert _upstream_models(transport) == ["Vendor/Mixed-Case:free"]
    assert pulse["traffic"]["model_remapped"] == 0


def test_a_near_miss_is_not_matched_fuzzily(auth_header: dict[str, str]) -> None:
    client, transport = _app()
    with client:
        response = _chat(client, auth_header, "other/mod")
    assert response.headers[HEADER] == "other/mod"
    assert _upstream_models(transport) == [ACTIVE]


def test_the_list_is_fetched_once_for_repeat_requests(auth_header: dict[str, str]) -> None:
    client, transport = _app()
    with client:
        for _ in range(3):
            _chat(client, auth_header, UNKNOWN)
    assert len([r for r in transport.requests if r.method == "GET"]) == 1


def test_a_role_override_is_not_second_guessed(
    auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL_BACKGROUND", "not/listed")
    client, transport = _app()
    with client:
        response = _chat(client, auth_header, UNKNOWN)
    assert HEADER not in response.headers
    assert _upstream_models(transport) == ["not/listed"]


def _cli_spy(kind: type):
    class Spy(kind):
        async def list_models(self):
            raise AssertionError("a CLI backend's model list must not be consulted")

    return Spy()


def test_a_subscription_cli_resolves_its_own_model_and_is_not_asked() -> None:
    from nexus.backends.subscription import CodexCliBackend
    from nexus.model_fallback import choose_model

    choice = asyncio.run(choose_model(_cli_spy(CodexCliBackend), "gpt-5.6-sol", {}))
    assert (choice.model, choice.remapped_from) == ("gpt-5.6-sol", None)


def test_grok_build_remaps_a_foreign_id_to_model_for(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OS3_BRIDGE_MODEL", raising=False)
    monkeypatch.delenv("NEXUS_MODEL", raising=False)
    from nexus.backends.subscription import GrokBuildBackend
    from nexus.config import model_for
    from nexus.model_fallback import HEADER as remap_header
    from nexus.model_fallback import choose_model, remap_headers

    with caplog.at_level(logging.INFO, logger="nexus"):
        choice = asyncio.run(choose_model(_cli_spy(GrokBuildBackend), ACTIVE, {}))
    served = model_for("grok-build")
    assert served and served != ACTIVE
    assert (choice.model, choice.remapped_from) == (served, ACTIVE)
    assert remap_headers(choice) == {remap_header: ACTIVE}
    lines = [r.getMessage() for r in caplog.records if "remapped" in r.getMessage()]
    assert lines == [f"model remapped backend=grok-build {ACTIVE} -> {served}"]


def test_grok_build_keeps_a_cli_token(caplog: pytest.LogCaptureFixture) -> None:
    from nexus.backends.subscription import GrokBuildBackend
    from nexus.model_fallback import choose_model, remap_headers

    with caplog.at_level(logging.INFO, logger="nexus"):
        choice = asyncio.run(choose_model(_cli_spy(GrokBuildBackend), "grok-4.7", {}))
    assert (choice.model, choice.remapped_from) == ("grok-4.7", None)
    assert remap_headers(choice) == {}
    assert [r for r in caplog.records if "remapped" in r.getMessage()] == []


def test_grok_build_keeps_a_foreign_id_when_fallback_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_MODEL_FALLBACK", "off")
    from nexus.backends.subscription import GrokBuildBackend
    from nexus.model_fallback import choose_model, remap_headers

    choice = asyncio.run(choose_model(_cli_spy(GrokBuildBackend), ACTIVE, {}))
    assert (choice.model, choice.remapped_from) == (ACTIVE, None)
    assert remap_headers(choice) == {}


@pytest.mark.parametrize("served", ["", None])
def test_grok_build_leaves_a_foreign_id_when_model_for_is_empty(
    monkeypatch: pytest.MonkeyPatch, served: str | None
) -> None:
    monkeypatch.setattr("nexus.model_fallback.model_for", lambda _backend_id: served)
    from nexus.backends.subscription import GrokBuildBackend
    from nexus.model_fallback import choose_model

    choice = asyncio.run(choose_model(_cli_spy(GrokBuildBackend), ACTIVE, {}))
    assert (choice.model, choice.remapped_from) == (ACTIVE, None)
