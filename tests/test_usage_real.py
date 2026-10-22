"""Real provider numbers: the ChatGPT plan usage endpoint, streamed token counts, ages."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import usage
from nexus.app import create_app
from nexus.backends.chatgpt import usage_url
from nexus.backends.openai_compat import stream_payload
from nexus.dashboard_usage import _or_cache, _plan_cache, usage_payload
from nexus.failover import FailoverState
from nexus.usage_sniff import SseUsage, client_wants_usage

from tests.test_chatgpt import auth_file  # noqa: F401
from tests.test_failover import Fake

NOW = 1_800_000_000.0
EMAIL = "someone@example.invalid"
# The shape GET /backend-api/wham/usage answered live (values here are made up).
WHAM = {
    "user_id": "user-NEVER-STORED-xxxxxxxxxxxx",
    "account_id": "acct-NEVER-STORED",
    "email": EMAIL,
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 7,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 1500,
            "reset_at": int(NOW + 1500),
        },
        "secondary_window": {
            "used_percent": 23,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 500000,
            "reset_at": int(NOW + 500000),
        },
    },
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}


@pytest.fixture(autouse=True)
def _fresh() -> None:
    usage._store = None
    _or_cache.clear()
    _plan_cache.clear()


def test_wham_usage_parses_both_windows_and_drops_identity() -> None:
    parsed = usage.parse_codex_usage(WHAM, NOW)
    assert parsed is not None
    assert parsed["from"] == usage.FROM_ENDPOINT
    primary, weekly = parsed["windows"]
    assert (primary["id"], primary["used_percent"], primary["window_minutes"]) == (
        "primary",
        7.0,
        300,
    )
    assert primary["label"] == "5-hour window" and primary["reset_at"] == int(NOW + 1500)
    assert (weekly["used_percent"], weekly["window_minutes"]) == (23.0, 10080)
    assert parsed["plan"] == "plus" and parsed["limit_reached"] is False
    assert parsed["credits"] == {"has_credits": False, "balance": 0.0}
    text = json.dumps(parsed)
    assert EMAIL not in text and "NEVER-STORED" not in text


def test_wham_usage_without_windows_is_no_reading() -> None:
    assert usage.parse_codex_usage({"plan_type": "plus"}, NOW) is None
    assert usage.parse_codex_usage([], NOW) is None
    reset_only = {"rate_limit": {"primary_window": {"used_percent": 3, "reset_after_seconds": 60}}}
    parsed = usage.parse_codex_usage(reset_only, NOW)
    assert parsed and parsed["windows"][0]["reset_at"] == int(NOW + 60)


def test_usage_url_follows_the_codex_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_CHATGPT_BASE", raising=False)
    assert usage_url() == "https://chatgpt.com/backend-api/wham/usage"
    monkeypatch.setenv("NEXUS_CHATGPT_BASE", "http://127.0.0.1:9/backend-api/codex")
    assert usage_url() == "http://127.0.0.1:9/backend-api/wham/usage"


def test_dashboard_reads_the_plan_endpoint_cached_and_labelled(
    monkeypatch: pytest.MonkeyPatch,
    auth_file: Path,  # noqa: F811
) -> None:
    token = json.loads(auth_file.read_text())["tokens"]["access_token"]
    seen: list[str] = []
    body = json.loads(json.dumps(WHAM))
    now = int(time.time())
    body["rate_limit"]["primary_window"]["reset_at"] = now + 1500
    body["rate_limit"]["secondary_window"]["reset_at"] = now + 500000

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith("/wham/usage")
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    payload = asyncio.run(
        usage_payload(active="chatgpt", failover=FailoverState(), transport=transport)
    )
    asyncio.run(usage_payload(active="chatgpt", failover=FailoverState(), transport=transport))
    assert seen == [f"Bearer {token}"]
    row = {r["id"]: r for r in payload["backends"]}["chatgpt"]
    assert row["source"] == "real" and row["from"] == "usage endpoint"
    assert row["age_s"] is not None and row["age_s"] <= 2
    assert [w["used_percent"] for w in row["windows"]] == [7.0, 23.0]
    assert row["plan"] == "plus" and "note" not in row
    text = json.dumps(payload)
    assert token not in text and EMAIL not in text and "NEVER-STORED" not in text


def test_plan_endpoint_failure_keeps_the_last_reading_with_a_note(
    monkeypatch: pytest.MonkeyPatch,
    auth_file: Path,  # noqa: F811
) -> None:
    reset = str(int(time.time()) + 900)
    usage.store().record_headers(
        "chatgpt",
        {"x-codex-primary-used-percent": "11", "x-codex-primary-reset-at": reset},
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(403, text="<html>no</html>"))
    payload = asyncio.run(
        usage_payload(active="chatgpt", failover=FailoverState(), transport=transport)
    )
    row = {r["id"]: r for r in payload["backends"]}["chatgpt"]
    assert row["note"] == "ChatGPT usage endpoint answered HTTP 403."
    assert row["from"] == "response headers" and row["windows"][0]["used_percent"] == 11.0
    assert "<html>" not in json.dumps(payload)


def test_plan_poll_can_be_turned_off(
    monkeypatch: pytest.MonkeyPatch,
    auth_file: Path,  # noqa: F811
) -> None:
    monkeypatch.setenv("NEXUS_CHATGPT_USAGE_POLL", "0")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the plan endpoint must not be called")

    asyncio.run(
        usage_payload(
            active="chatgpt", failover=FailoverState(), transport=httpx.MockTransport(handler)
        )
    )


def test_no_login_means_no_plan_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no login on file: nothing to read")

    asyncio.run(
        usage_payload(
            active="nous", failover=FailoverState(), transport=httpx.MockTransport(handler)
        )
    )


def test_old_reading_is_marked_with_its_age_and_unknown_level(tmp_path: Path) -> None:
    clock = [NOW]
    store = usage.UsageStore(tmp_path / "u.json", clock=lambda: clock[0])
    store.record_headers(
        "nous",
        {
            "x-ratelimit-limit-requests": "50",
            "x-ratelimit-remaining-requests": "43",
            "x-ratelimit-reset-requests": "30s",
        },
    )
    clock[0] = NOW + 5 * 3600
    real = store.snapshot()["nous"]["real"]
    assert real["age_s"] == 5 * 3600 and real["from"] == "response headers"
    assert real["windows"][0]["stale"] is True
    from nexus.dashboard_usage import _row

    row = _row("nous", store.snapshot()["nous"], active="chatgpt", cooling={})
    assert row["windows"][0]["level"] == "unknown" and row["age_s"] == 5 * 3600


def test_every_call_refreshes_the_reading(tmp_path: Path) -> None:
    clock = [NOW]
    store = usage.UsageStore(tmp_path / "u.json", clock=lambda: clock[0])
    headers = {"x-ratelimit-limit-requests": "50", "x-ratelimit-remaining-requests": "49"}
    store.record_headers("nous", headers)
    clock[0] = NOW + 120
    store.record_headers("nous", {**headers, "x-ratelimit-remaining-requests": "40"})
    real = store.snapshot()["nous"]["real"]
    assert real["updated_at"] == int(NOW + 120) and real["windows"][0]["remaining"] == 40


def _chunk(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


DELTA = {"choices": [{"index": 0, "delta": {"content": "hi"}}]}
USAGE_ONLY = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


def test_sniff_reads_include_usage_and_drops_it_when_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    noted: list[tuple] = []
    monkeypatch.setattr(
        "nexus.usage_sniff.note_tokens",
        lambda *, prompt=None, completion=None: noted.append((prompt, completion)),
    )
    raw = _chunk(DELTA) + _chunk(USAGE_ONLY) + b"data: [DONE]\n\n"
    sniff = SseUsage(drop_usage_only=True)
    out = b"".join(sniff.feed(raw[i : i + 7]) for i in range(0, len(raw), 7)) + sniff.finish()
    assert out == _chunk(DELTA) + b"data: [DONE]\n\n"
    assert noted == [(12, 3)]

    kept = SseUsage(drop_usage_only=False)
    assert kept.feed(raw) + kept.finish() == raw


def test_sniff_reads_a_final_chunk_and_anthropic_events() -> None:
    final = {
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 5},
    }
    sniff = SseUsage(drop_usage_only=True)
    assert sniff.feed(_chunk(final)) == _chunk(final)
    assert (sniff.prompt, sniff.completion) == (15, 5)

    messages = SseUsage()
    messages.feed(
        b"event: message_start\n"
        + _chunk(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 40, "output_tokens": 1}},
            }
        )
    )
    messages.feed(
        b"event: message_delta\n" + _chunk({"type": "message_delta", "usage": {"output_tokens": 9}})
    )
    assert (messages.prompt, messages.completion) == (40, 9)


def test_sniff_without_usage_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr("nexus.usage_sniff.note_tokens", lambda **_: called.append(1))
    sniff = SseUsage()
    assert sniff.feed(_chunk(DELTA) + b": keepalive\n\n") == _chunk(DELTA) + b": keepalive\n\n"
    assert sniff.finish() == b"" and called == []


def test_stream_payload_asks_for_usage_without_mutating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_STREAM_USAGE")
    payload = {"model": "m", "stream": True, "stream_options": {"x": 1}}
    asked = stream_payload(payload)
    assert asked["stream_options"] == {"x": 1, "include_usage": True}
    assert payload["stream_options"] == {"x": 1} and not client_wants_usage(payload)
    assert client_wants_usage(asked)
    monkeypatch.setenv("NEXUS_STREAM_USAGE", "0")
    assert stream_payload(payload) is payload


def test_streamed_chat_records_provider_tokens(
    monkeypatch: pytest.MonkeyPatch, bridge_key: str
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    fake = Fake("nous", chunks=[_chunk(DELTA), _chunk(USAGE_ONLY)])
    client = TestClient(create_app(backend=fake))
    headers = {"Authorization": f"Bearer {bridge_key}"}
    request = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    response = client.post("/v1/chat/completions", headers=headers, json=request)
    assert response.status_code == 200
    assert '"choices": []' not in response.text and "data: [DONE]" in response.text
    asked = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={**request, "stream_options": {"include_usage": True}},
    )
    assert '"choices": []' in asked.text
    rows = {r["id"]: r for r in client.get("/dashboard/usage").json()["backends"]}
    estimate = rows["nous"]["estimate"]
    assert estimate["requests_1h"] == 2 and estimate["tokens_1h"] == 30
    assert estimate["token_reports_1h"] == 2
    streamed = [
        e for e in client.get("/dashboard/pulse").json()["entries"] if e["route"].startswith("POST")
    ]
    assert [(e["prompt_tokens"], e["completion_tokens"]) for e in streamed] == [(12, 3), (12, 3)]


def test_call_without_usage_is_counted_as_not_reported(
    monkeypatch: pytest.MonkeyPatch, bridge_key: str
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    client = TestClient(create_app(backend=Fake("nous")))
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bridge_key}"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    estimate = {r["id"]: r for r in client.get("/dashboard/usage").json()["backends"]}["nous"][
        "estimate"
    ]
    assert estimate["requests_1h"] == 1 and estimate["token_reports_1h"] == 0


@pytest.mark.parametrize("stream", [True, False])
def test_chatgpt_plan_tokens_come_from_response_completed(
    monkeypatch: pytest.MonkeyPatch,
    auth_file: Path,  # noqa: F811
    bridge_key: str,
    stream: bool,
) -> None:
    """The case the dashboard showed as 0 tokens: a streamed ChatGPT plan reply."""
    from tests.test_chatgpt import Upstream, _client, _sse, _text_events

    monkeypatch.setenv("NEXUS_CHATGPT_USAGE_POLL", "0")
    upstream = Upstream([(200, _sse(_text_events("hello")))])
    with _client(upstream) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"messages": [{"role": "user", "content": "hi"}], "stream": stream},
        )
        assert response.status_code == 200
        rows = {r["id"]: r for r in client.get("/dashboard/usage").json()["backends"]}
    estimate = rows["chatgpt"]["estimate"]
    assert estimate["tokens_1h"] == 7 and estimate["token_reports_1h"] == 1
