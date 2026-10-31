"""Cross-provider failover at a usage limit. Fake backends, an injectable clock, no sleeps."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexus import status
from nexus.app import create_app
from nexus.backends.base import ChatUpstream
from nexus.errors import (
    OUT_OF_QUOTA,
    RATE_LIMITED,
    UNSUPPORTED,
    GatewayError,
    classify_failure,
    quota_text,
)
from nexus.failover import (
    FailoverBackend,
    FailoverState,
    chain_warnings,
    effective_chain,
    fallback_config,
    with_fallback,
)

CHAIN = "codex,claude-code,nous,openrouter"
CLI_IDS = {"codex", "claude-code", "copilot", "grok-build", "antigravity"}

# Sample strings are written in the style of what each tool is reported to print. Vendors
# reword these; the table in errors.py is best effort, so these pin our patterns, not theirs.
QUOTA_TEXT = [
    ("codex", "ERROR: You've hit your usage limit. Upgrade to Plus or try again at 3:42 PM."),
    ("codex", '{"type":"error","message":"usage_limit_reached"}'),
    ("claude-code", "Claude AI usage limit reached|1760000000"),
    ("claude-code", "5-hour limit reached ∙ resets 3pm"),
    ("claude-code", "Weekly limit reached ∙ resets Mon 9am"),
    ("claude-code", "You've hit your limit · resets 2am"),
    ("copilot", "You have exceeded your premium request allowance."),
    ("copilot", "Quota exceeded for this billing period"),
    ("grok-build", "Quota exceeded for this account"),
    ("antigravity", "RESOURCE_EXHAUSTED: Quota exceeded for quota metric"),
    ("antigravity", "You have exhausted your capacity on this model."),
    ("codex", "subscription rate limit hit"),
    (
        "openai",
        '{"error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}',
    ),
    ("anthropic", '{"error":{"message":"Your credit balance is too low to access the API"}}'),
    (
        "openrouter",
        '{"error":{"message":"This request requires more credits, or fewer max_tokens"}}',
    ),
    ("nous", "usage limit exceeded for this key"),
]
RATE_TEXT = [
    ("grok-build", "Rate limit reached for grok-build"),
    ("antigravity", "RESOURCE_EXHAUSTED: too many requests, retry in 20s"),
    ("codex", "exceeded retry limit, last status: 429 Too Many Requests"),
]
NOT_QUOTA_TEXT = [
    ("codex", "Not logged in"),
    ("codex", "codex exploded"),
    ("codex", "You have a rate limit of cheese"),
    ("claude-code", "Invalid API key · Please run /login"),
    ("claude-code", "Prompt is too long"),
    ("grok-build", "Not signed in. To authenticate without a browser, run: grok login"),
    ("copilot", "Model not supported"),
    ("openrouter", '{"error":{"message":"No endpoints found for that model"}}'),
    ("openai", '{"error":{"message":"Incorrect API key provided"}}'),
    ("grok-build", "Rate limit reached for grok-build"),
    ("antigravity", "RESOURCE_EXHAUSTED: too many requests"),
    ("grok-build", "failed to read quota.json: no such file"),
    ("copilot", "premium models are listed in the docs"),
    ("anthropic", '{"error":{"message":"max_tokens: 0 is invalid"}}'),
]


@pytest.mark.parametrize(("backend", "text"), QUOTA_TEXT)
def test_quota_text_is_recognised(backend: str, text: str) -> None:
    assert quota_text(text, backend)


@pytest.mark.parametrize(("backend", "text"), RATE_TEXT)
def test_rate_text_is_a_rate_limit_not_quota(backend: str, text: str) -> None:
    assert not quota_text(text, backend)
    error = GatewayError(502, text, "api_error", f"{backend}_failed")
    assert classify_failure(error) == RATE_LIMITED


@pytest.mark.parametrize(("backend", "text"), NOT_QUOTA_TEXT)
def test_other_text_is_not_quota(backend: str, text: str) -> None:
    assert not quota_text(text, backend)


def test_a_cli_row_does_not_apply_to_another_backend() -> None:
    text = "Claude AI usage limit reached"
    assert quota_text(text, "claude-code")
    assert quota_text(text, "codex")
    assert not quota_text("5-hour limit reached", "codex")


def _upstream_error(
    backend: str, status: int, body: str, *, code: str | None = None
) -> GatewayError:
    detail = {"backend": backend, "upstream_status": status, "upstream_body": body}
    return GatewayError(
        502,
        f"{backend} upstream error",
        "upstream_error",
        code or f"{backend}_upstream_error",
        extra={backend: detail, "upstream": detail},
    )


def _cli_quota(backend: str = "codex") -> GatewayError:
    return GatewayError(429, "usage limit reached", "rate_limit_error", f"{backend}_usage_limit")


CLASSIFY = [
    ("cli usage limit", _cli_quota("claude-code"), OUT_OF_QUOTA),
    ("api 429 bare", _upstream_error("nous", 429, ""), RATE_LIMITED),
    (
        "api 429 rate body",
        _upstream_error("openai", 429, '{"error":{"code":"rate_limit_exceeded"}}'),
        RATE_LIMITED,
    ),
    (
        "api 429 body",
        _upstream_error("openai", 429, '{"error":{"code":"insufficient_quota"}}'),
        OUT_OF_QUOTA,
    ),
    ("openrouter 402", _upstream_error("openrouter", 402, "requires more credits"), OUT_OF_QUOTA),
    ("anthropic 400", _upstream_error("anthropic", 400, "credit balance is too low"), OUT_OF_QUOTA),
    ("api 401", _upstream_error("openai", 401, "invalid key; quota irrelevant"), None),
    ("api 403", _upstream_error("xai", 403, "forbidden"), None),
    ("api 400", _upstream_error("openai", 400, "unknown parameter"), None),
    ("api 404", _upstream_error("openrouter", 404, "no such model"), UNSUPPORTED),
    ("api bad model", _upstream_error("openrouter", 400, "x is not a valid model ID"), UNSUPPORTED),
    ("cli tools", GatewayError(400, "no tools", "x", "tools_not_supported"), UNSUPPORTED),
    ("cli images", GatewayError(400, "no images", "x", "images_not_supported"), UNSUPPORTED),
    ("cli image url", GatewayError(400, "no urls", "x", "image_url_unsupported"), UNSUPPORTED),
    ("cli quota word", GatewayError(502, "quota exceeded", "x", "grok-build_failed"), OUT_OF_QUOTA),
    ("cli rate word", GatewayError(502, "rate limit hit", "x", "grok-build_failed"), RATE_LIMITED),
    ("cli mentions quota", GatewayError(502, "see quota docs", "x", "grok-build_failed"), None),
    ("api 500", _upstream_error("openai", 500, "boom"), None),
    ("timeout", GatewayError(504, "timed out", "upstream_error", "openai_timeout"), None),
    ("unreachable", GatewayError(502, "down", "upstream_error", "nous_unreachable"), None),
    ("cli login", GatewayError(503, "not logged in", "x", "codex_login_required"), None),
    ("cli missing", GatewayError(503, "no cli", "x", "codex_cli_missing"), None),
    ("cli failed", GatewayError(502, "exploded", "api_error", "codex_failed"), None),
    ("bad request", GatewayError(400, "bad", "invalid_request_error", "invalid_messages"), None),
]


@pytest.mark.parametrize(("label", "exc", "expected"), CLASSIFY, ids=[c[0] for c in CLASSIFY])
def test_classify_failure(label: str, exc: GatewayError, expected: str | None) -> None:
    assert classify_failure(exc) == expected


class Fake:
    def __init__(
        self,
        backend_id: str,
        *,
        present: bool = True,
        fail: GatewayError | None = None,
        chunks: list[bytes] | None = None,
        end_early: bool = False,
        raise_after_chunks: GatewayError | None = None,
    ) -> None:
        self.id = backend_id
        self.present = present
        self.fail = fail
        self.chunks = chunks
        self.end_early = end_early
        self.raise_after_chunks = raise_after_chunks
        self.calls: list[dict[str, Any]] = []

    def describe(self) -> dict[str, Any]:
        if self.id in CLI_IDS:
            return {
                "id": self.id,
                "kind": "cli",
                "cli_present": self.present,
                "login": "ok" if self.present else "unchecked",
                "ready": self.present,
                "hint": "ok",
            }
        return {
            "id": self.id,
            "kind": "rest",
            "key_present": self.present,
            "ready": self.present,
            "hint": "ok",
        }

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    async def model_reachable(self, model: str) -> bool | None:
        return True

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        self.calls.append(dict(payload))
        if self.fail is not None:
            raise self.fail
        if stream:
            return ChatUpstream(200, "text/event-stream", stream=self._stream())
        body = {
            "id": "x",
            "object": "chat.completion",
            "model": payload.get("model"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": f"from {self.id}"}}
            ],
        }
        return ChatUpstream(200, "application/json", body=json.dumps(body).encode())

    async def _stream(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks or [_sse(f"from {self.id}")]:
            yield chunk
        if self.raise_after_chunks is not None:
            raise self.raise_after_chunks
        if not self.end_early:
            yield b"data: [DONE]\n\n"


def _sse(text: str) -> bytes:
    return b'data: {"choices":[{"index":0,"delta":{"content":"' + text.encode() + b'"}}]}\n\n'


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    def __init__(
        self, fakes: dict[str, Fake], clock: Clock, client: TestClient, headers: dict[str, str]
    ) -> None:
        self.fakes = fakes
        self.clock = clock
        self.client = client
        self.headers = headers

    def chat(self, **extra: Any) -> Any:
        body = {"messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}], **extra}
        return self.client.post("/v1/chat/completions", headers=self.headers, json=body)

    def health(self) -> dict[str, Any]:
        return self.client.get("/health").json()

    @property
    def calls(self) -> dict[str, int]:
        return {name: len(fake.calls) for name, fake in self.fakes.items()}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, bridge_key: str):
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", CHAIN)

    def build(**overrides: Fake) -> Rig:
        fakes = {name: Fake(name) for name in ("codex", "claude-code", "nous", "openrouter")}
        fakes.update(overrides)
        clock = Clock()
        app = create_app(backend=fakes["codex"])
        app.state.failover = FailoverState(clock=clock, wall=lambda: 1_800_000_000 + clock.now)
        app.state.backend_factory = fakes.__getitem__
        client = TestClient(app, raise_server_exceptions=False)
        return Rig(fakes, clock, client, {"Authorization": f"Bearer {bridge_key}"})

    return build


def test_quota_on_the_primary_moves_to_the_next_backend(rig) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    response = r.chat(model="gpt-5.6-sol")
    assert response.status_code == 200
    assert "from claude-code" in response.text
    assert r.calls == {"codex": 1, "claude-code": 1, "nous": 0, "openrouter": 0}
    assert r.fakes["codex"].calls[0]["model"] == "gpt-5.6-sol"
    assert r.fakes["claude-code"].calls[0]["model"] == "claude-sonnet-4-5"


def test_the_chain_is_walked_in_order_across_providers(rig) -> None:
    r = rig(
        codex=Fake("codex", fail=_cli_quota()),
        **{"claude-code": Fake("claude-code", fail=_cli_quota("claude-code"))},
        nous=Fake("nous", fail=_upstream_error("nous", 429, "")),
    )
    response = r.chat()
    assert response.status_code == 200
    assert "from openrouter" in response.text
    assert r.calls == {"codex": 1, "claude-code": 1, "nous": 1, "openrouter": 1}
    assert r.health()["fallback"]["failovers"] == 3


def test_unconfigured_backends_are_skipped(rig) -> None:
    r = rig(
        codex=Fake("codex", fail=_cli_quota()),
        **{"claude-code": Fake("claude-code", present=False)},
        nous=Fake("nous", present=False),
    )
    response = r.chat()
    assert "from openrouter" in response.text
    assert r.calls == {"codex": 1, "claude-code": 0, "nous": 0, "openrouter": 1}


@pytest.mark.parametrize(
    "error",
    [
        _upstream_error("codex", 401, "bad key"),
        _upstream_error("codex", 403, "forbidden"),
        _upstream_error("codex", 400, "bad request"),
        GatewayError(503, "not logged in", "unavailable", "codex_login_required"),
        GatewayError(504, "timed out", "api_error", "codex_timeout"),
        GatewayError(502, "exploded", "api_error", "codex_failed"),
    ],
)
def test_only_quota_fails_over(rig, error: GatewayError) -> None:
    r = rig(codex=Fake("codex", fail=error))
    response = r.chat()
    assert response.status_code == error.status_code
    assert response.json()["error"]["code"] == error.code
    assert r.calls == {"codex": 1, "claude-code": 0, "nous": 0, "openrouter": 0}
    assert r.health()["fallback"]["failovers"] == 0


def test_no_failover_once_the_stream_has_started(rig) -> None:
    # What a CLI stream does when the limit lands after its first text: it just stops.
    r = rig(codex=Fake("codex", chunks=[_sse("par")], end_early=True))
    response = r.chat(stream=True)
    assert b"par" in response.content
    assert r.calls == {"codex": 1, "claude-code": 0, "nous": 0, "openrouter": 0}
    fallback = r.health()["fallback"]
    assert fallback["failovers"] == 0 and fallback["cooling"] == []


def test_a_stream_that_raises_after_its_first_chunk_is_not_retried() -> None:
    codex = Fake("codex", chunks=[_sse("par")], raise_after_chunks=_cli_quota())
    other = Fake("claude-code")
    backend = FailoverBackend(
        codex, ["codex", "claude-code"], FailoverState(), 900.0, {"claude-code": other}.__getitem__
    )

    async def run() -> list[bytes]:
        upstream = await backend.open_chat({"model": "m"}, stream=True)
        assert upstream.stream is not None
        got: list[bytes] = []
        with pytest.raises(GatewayError):
            async for chunk in upstream.stream:
                got.append(chunk)
        return got

    assert asyncio.run(run()) == [_sse("par")]
    assert other.calls == []


def test_quota_before_the_first_byte_fails_over_a_stream(rig) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    response = r.chat(stream=True)
    assert response.status_code == 200
    assert b"from claude-code" in response.content
    assert response.content.count(b"data: [DONE]") == 1


def test_messages_route_fails_over_too(rig) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    response = r.client.post(
        "/v1/messages",
        headers=r.headers,
        json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert "from claude-code" in response.text
    assert r.calls["claude-code"] == 1


def test_all_backends_exhausted_answers_429_with_a_hint(rig) -> None:
    quota = {
        "claude-code": Fake("claude-code", fail=_cli_quota("claude-code")),
        "nous": Fake("nous", fail=_upstream_error("nous", 429, "")),
        "openrouter": Fake(
            "openrouter",
            fail=_upstream_error("openrouter", 402, "This request requires more credits"),
        ),
    }
    r = rig(codex=Fake("codex", fail=_cli_quota()), **quota)
    response = r.chat()
    error = response.json()["error"]
    assert response.status_code == 429
    assert error["code"] == "fallback_exhausted"
    assert "codex" in error["hint"] and "NEXUS_FALLBACK" in error["hint"]
    assert error["fallback"]["tried"] == ["codex", "claude-code", "nous", "openrouter"]


def test_a_lone_quota_error_with_nothing_to_fall_to_is_returned_unchanged(rig) -> None:
    absent = {n: Fake(n, present=False) for n in ("claude-code", "nous", "openrouter")}
    r = rig(codex=Fake("codex", fail=_cli_quota()), **absent)
    response = r.chat()
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "codex_usage_limit"


def test_cooldown_skips_the_primary_then_returns_to_it(rig) -> None:
    codex = Fake("codex", fail=_cli_quota())
    r = rig(codex=codex)
    assert "from claude-code" in r.chat().text
    assert r.calls["codex"] == 1

    r.clock.now += 899
    assert "from claude-code" in r.chat().text
    assert r.calls["codex"] == 1

    codex.fail = None
    r.clock.now += 2
    assert "from codex" in r.chat().text
    assert r.calls == {"codex": 2, "claude-code": 2, "nous": 0, "openrouter": 0}


def test_cooldown_is_configurable(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK_COOLDOWN", "60")
    codex = Fake("codex", fail=_cli_quota())
    r = rig(codex=codex)
    r.chat()
    codex.fail = None
    r.clock.now += 61
    assert "from codex" in r.chat().text


def test_when_everything_is_cooling_the_primary_answers_as_before(rig) -> None:
    quota = {
        "claude-code": Fake("claude-code", fail=_cli_quota("claude-code")),
        "nous": Fake("nous", fail=_upstream_error("nous", 429, "")),
        "openrouter": Fake("openrouter", fail=_upstream_error("openrouter", 429, "")),
    }
    r = rig(codex=Fake("codex", fail=_cli_quota()), **quota)
    assert r.chat().status_code == 429
    before = dict(r.calls)
    response = r.chat()
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "codex_usage_limit"
    assert r.calls["codex"] == before["codex"] + 1
    assert r.calls["nous"] == before["nous"]


def test_state_with_an_injected_clock() -> None:
    clock = Clock()
    state = FailoverState(clock=clock, wall=lambda: 1_800_000_000 + clock.now)
    until = state.start_cooldown("codex", 900)
    assert until == "2027-01-15T08:31:40Z"
    assert state.cooling("codex") and state.cooling_names() == {"codex"}
    clock.now += 900
    assert not state.cooling("codex") and state.cooling_names() == set()


def test_health_shows_the_chain_the_cooldown_and_the_counter(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    real = status.describe_catalog
    monkeypatch.setattr(
        status, "describe_catalog", lambda: {**real(), "codex": r.fakes["codex"].describe()}
    )
    before = r.health()
    assert before["service"] == "nexus"
    assert before["fallback"]["enabled"] is True
    assert before["fallback"]["chain"] == CHAIN.split(",")
    assert before["fallback"]["active"] == "codex"
    assert before["fallback"]["failovers"] == 0

    r.chat()
    after = r.health()
    fallback = after["fallback"]
    assert fallback["primary"] == "codex"
    assert fallback["active"] == "claude-code"
    assert fallback["cooldown_s"] == 900.0
    assert fallback["failovers"] == 1
    assert fallback["cooling"] == [
        {
            "backend": "codex",
            "reason": "out_of_quota",
            "until": "2027-01-15T08:31:40Z",
            "remaining_s": 900,
        }
    ]
    assert fallback["last_failover"]["from"] == "codex"
    assert fallback["last_failover"]["to"] == "claude-code"
    assert after["backends"]["codex"]["state"] == "out_of_quota"

    r.clock.now += 901
    healed = r.health()["fallback"]
    assert healed["cooling"] == [] and healed["active"] == "codex"


def test_pulse_carries_the_counter_and_the_serving_backend(rig) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    r.chat()
    body = r.client.get("/dashboard/pulse").json()
    assert body["traffic"]["failovers"] == 1
    assert body["fallback"]["active"] == "claude-code"
    assert body["entries"][0]["backend"] == "claude-code"
    assert body["entries"][0]["status"] == 200


def test_one_log_line_per_failover_without_prompt_text(
    rig, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    with caplog.at_level(logging.INFO, logger="nexus"):
        r.chat()
    lines = [rec.getMessage() for rec in caplog.records if "failover" in rec.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("failover codex -> claude-code reason=out_of_quota cooldown_until=")
    assert all("SECRET-PROMPT-TEXT" not in rec.getMessage() for rec in caplog.records)


def test_off_by_default_and_a_single_backend_is_untouched(
    monkeypatch: pytest.MonkeyPatch, bridge_key: str
) -> None:
    monkeypatch.delenv("OS3_BRIDGE_FALLBACK", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    codex = Fake("codex", fail=_cli_quota())
    app = create_app(backend=codex)
    app.state.backend_factory = lambda name: pytest.fail(f"built {name}")
    assert with_fallback(codex, app.state.failover, app.state.backend_factory) is codex
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        fallback = client.get("/health").json()["fallback"]
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "codex_usage_limit"
    assert fallback["enabled"] is False and fallback["chain"] == []


def test_a_chain_naming_only_the_active_backend_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "codex")
    codex = Fake("codex")
    assert with_fallback(codex, FailoverState(), lambda name: codex) is codex


def test_the_active_backend_leads_whatever_the_chain_says(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "nous, claude-code codex agy")
    config = fallback_config()
    assert config.chain == ("nous", "claude-code", "codex", "antigravity")
    assert effective_chain(config, "codex") == ["codex", "nous", "claude-code", "antigravity"]


def test_the_chain_can_come_from_a_file_and_env_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    path = tmp_path / "fallback.json"
    path.write_text(json.dumps({"chain": ["codex", "nous"], "cooldown_s": 120}))
    monkeypatch.delenv("OS3_BRIDGE_FALLBACK", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK_FILE", str(path))
    config = fallback_config()
    assert (config.chain, config.cooldown_s, config.source) == (("codex", "nous"), 120.0, "file")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "openrouter")
    assert fallback_config().chain == ("openrouter",)
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "off")
    assert fallback_config().chain == ()


def test_bad_config_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "codex,sk-live-abc123def,nope")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK_COOLDOWN", "soon")
    config = fallback_config()
    assert config.chain == ("codex",)
    assert config.cooldown_s == 900.0
    text = " ".join(config.problems)
    assert "entry 2" in text and "entry 3" in text and "sk-live" not in text
    assert "COOLDOWN" in text
    monkeypatch.delenv("OS3_BRIDGE_FALLBACK")
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK_FILE", str(path))
    assert "could not be read" in fallback_config().problems[0]


@pytest.mark.parametrize("stream", [False, True])
def test_a_real_cli_usage_limit_fails_over_to_an_api_backend(
    stream: bool, monkeypatch: pytest.MonkeyPatch, bridge_key: str, tmp_path
) -> None:
    from tests.test_subscription import _install_cli

    _install_cli(tmp_path, monkeypatch, mode="limit")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "codex,openrouter")
    app = create_app()
    api = Fake("openrouter")
    app.state.backend_factory = {"openrouter": api}.__getitem__
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"messages": [{"role": "user", "content": "hi"}], "stream": stream},
        )
        fallback = client.get("/health").json()["fallback"]
    assert response.status_code == 200
    assert "from openrouter" in response.text
    assert len(api.calls) == 1
    assert [c["backend"] for c in fallback["cooling"]] == ["codex"]


def _rate_limited(backend: str, retry_after: float | None = None) -> GatewayError:
    exc = _upstream_error(backend, 429, '{"error":{"code":"rate_limit_exceeded"}}')
    exc.retry_after = retry_after
    return exc


def test_a_rate_limit_cools_down_for_retry_after_not_the_quota_window(rig) -> None:
    codex = Fake("codex", fail=_rate_limited("codex", retry_after=7))
    r = rig(codex=codex)
    assert "from claude-code" in r.chat().text
    entry = r.health()["fallback"]["cooling"][0]
    assert (entry["backend"], entry["reason"], entry["remaining_s"]) == ("codex", "rate_limited", 7)
    assert r.health()["backends"]["codex"]["state"] != "out_of_quota"
    codex.fail = None
    r.clock.now += 8
    assert "from codex" in r.chat().text


def test_a_rate_limit_without_retry_after_waits_a_minute(rig) -> None:
    r = rig(codex=Fake("codex", fail=_rate_limited("codex")))
    r.chat()
    assert r.health()["fallback"]["cooling"][0]["remaining_s"] == 60
    assert r.health()["fallback"]["last_failover"]["reason"] == "rate_limited"


def test_a_long_retry_after_is_capped_at_the_quota_cooldown(rig) -> None:
    r = rig(codex=Fake("codex", fail=_rate_limited("codex", retry_after=86400)))
    r.chat()
    assert r.health()["fallback"]["cooling"][0]["remaining_s"] == 900


def test_a_quota_body_on_a_429_gets_the_long_cooldown(rig) -> None:
    body = '{"error":{"type":"insufficient_quota"}}'
    quota = _upstream_error("openai", 429, body)
    quota.retry_after = 5
    r = rig(codex=Fake("codex", fail=quota))
    r.chat()
    cooling = r.health()["fallback"]["cooling"][0]
    assert (cooling["reason"], cooling["remaining_s"]) == ("out_of_quota", 900)


def test_retry_after_is_read_from_the_upstream_response_and_not_serialized(
    openrouter_key: str,
) -> None:
    import httpx
    from nexus.backends.openrouter import OpenRouterBackend

    from tests.transport import HandlerTransport

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, json={"error": "slow down"})

    backend = OpenRouterBackend(transport=HandlerTransport(handler))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(backend.open_chat({"model": "m", "messages": []}, stream=False))
    assert caught.value.retry_after == 12
    assert "retry" not in json.dumps(caught.value.as_dict()).lower().replace("retrying", "")
    assert classify_failure(caught.value) == RATE_LIMITED


def test_retry_after_parsing() -> None:
    from nexus.backends.upstream import parse_retry_after

    assert parse_retry_after("30") == 30
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_the_global_model_and_the_clients_model_apply_only_to_the_primary(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_MODEL", "primary-only-model")
    r = rig(
        codex=Fake("codex", fail=_cli_quota()),
        **{"claude-code": Fake("claude-code", fail=_cli_quota("claude-code"))},
    )
    r.chat(model="client-asked-for-this")
    sent = {name: [c["model"] for c in fake.calls] for name, fake in r.fakes.items()}
    assert sent["codex"] == ["client-asked-for-this"]
    assert sent["claude-code"] == ["claude-sonnet-4-5"]
    assert sent["nous"] == ["meituan/longcat-2.5-preview:free"]
    assert "primary-only-model" not in json.dumps(sent)


def test_a_saved_model_applies_to_its_own_backend_only(
    rig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / "backend-choice").write_text("nous\n")
    (tmp_path / "model-choice").write_text("saved/nous-model\n")
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    r.chat()
    assert r.fakes["claude-code"].calls[0]["model"] == "claude-sonnet-4-5"


def test_the_pulse_records_the_model_the_fallback_ran(rig) -> None:
    r = rig(codex=Fake("codex", fail=_cli_quota()))
    r.chat(model="gpt-5.6-sol")
    entry = r.client.get("/dashboard/pulse").json()["entries"][0]
    assert (entry["backend"], entry["model"]) == ("claude-code", "claude-sonnet-4-5")


TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
IMAGE = [
    {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    }
]


def _chain(monkeypatch: pytest.MonkeyPatch, chain: str, backend: str = "nous") -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", backend)
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", chain)


def test_a_tools_request_is_not_sent_to_a_cli_in_the_chain(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chain(monkeypatch, "nous,claude-code,openrouter")
    r = rig(nous=Fake("nous", fail=_upstream_error("nous", 429, "insufficient_quota")))
    app = create_app(backend=r.fakes["nous"])
    app.state.backend_factory = r.fakes.__getitem__
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=r.headers,
            json={"messages": [{"role": "user", "content": "x"}], "tools": TOOLS},
        )
    assert "from openrouter" in response.text
    assert r.calls == {"codex": 0, "claude-code": 0, "nous": 1, "openrouter": 1}


def test_an_image_request_is_not_sent_to_a_backend_that_refuses_images(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chain(monkeypatch, "codex,grok-build,openrouter", backend="codex")
    r = rig(codex=Fake("codex", fail=_cli_quota()), **{"grok-build": Fake("grok-build")})
    response = r.chat(messages=IMAGE)
    assert "from openrouter" in response.text
    assert r.calls["grok-build"] == 0


@pytest.mark.parametrize(
    "refusal",
    [
        GatewayError(400, "no urls", "invalid_request_error", "image_url_unsupported"),
        GatewayError(400, "no tools", "invalid_request_error", "tools_not_supported"),
        _upstream_error("claude-code", 404, "model not found"),
    ],
)
def test_a_capability_refusal_on_a_fallback_skips_it_without_a_cooldown(
    rig, refusal: GatewayError
) -> None:
    r = rig(
        codex=Fake("codex", fail=_cli_quota()),
        **{"claude-code": Fake("claude-code", fail=refusal)},
    )
    response = r.chat()
    assert "from nous" in response.text
    assert r.calls == {"codex": 1, "claude-code": 1, "nous": 1, "openrouter": 0}
    fallback = r.health()["fallback"]
    assert [c["backend"] for c in fallback["cooling"]] == ["codex"]
    assert fallback["failovers"] == 1
    assert fallback["last_failover"]["to"] == "nous"


def test_nothing_is_counted_or_logged_when_no_fallback_can_serve(
    rig, caplog: pytest.LogCaptureFixture
) -> None:
    refusal = GatewayError(400, "no tools", "invalid_request_error", "tools_not_supported")
    r = rig(
        codex=Fake("codex", fail=_cli_quota()),
        **{"claude-code": Fake("claude-code", fail=refusal)},
        nous=Fake("nous", present=False),
        openrouter=Fake("openrouter", present=False),
    )
    with caplog.at_level(logging.INFO, logger="nexus"):
        response = r.chat()
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "codex_usage_limit"
    assert r.health()["fallback"]["failovers"] == 0
    assert not [rec for rec in caplog.records if "failover" in rec.getMessage()]


def test_the_primary_s_own_capability_error_is_returned_as_before(rig) -> None:
    refusal = GatewayError(400, "no tools", "invalid_request_error", "tools_not_supported")
    r = rig(codex=Fake("codex", fail=refusal))
    response = r.chat()
    assert response.status_code == 400
    assert r.calls["claude-code"] == 0


def test_the_backend_header_appears_only_when_a_failover_answered(rig) -> None:
    ok = rig()
    assert "x-os3-backend" not in ok.chat().headers
    moved = rig(codex=Fake("codex", fail=_cli_quota()))
    assert moved.chat().headers["x-os3-backend"] == "claude-code"
    assert moved.chat(stream=True).headers["x-os3-backend"] == "claude-code"
    messages = moved.client.post(
        "/v1/messages",
        headers=moved.headers,
        json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert messages.headers["x-os3-backend"] == "claude-code"


def test_no_header_when_fallback_is_off(monkeypatch: pytest.MonkeyPatch, bridge_key: str) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "codex")
    app = create_app(backend=Fake("codex"))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200 and "x-os3-backend" not in response.headers


def test_a_chain_from_a_subscription_to_a_billed_backend_is_flagged(rig) -> None:
    warnings = rig().health()["fallback"]["warnings"]
    assert len(warnings) == 1
    assert "codex" in warnings[0] and "nous, openrouter" in warnings[0]
    assert chain_warnings(["openrouter", "nous"]) == []
    assert chain_warnings(["nous", "codex"]) == []
    assert chain_warnings(["codex", "claude-code"]) == []
    assert chain_warnings(["codex"]) == []


def test_doctor_sh_prints_the_chain_and_the_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import subprocess
    from pathlib import Path

    env = dict(os.environ)
    env.update(
        OS3_BRIDGE_FALLBACK="codex,openrouter",
        OS3_BRIDGE_BACKEND="openrouter",
        OS3_BRIDGE_KEY_FILE=str(tmp_path / "bridge.key"),
        OS3_BRIDGE_OPENROUTER_KEY_FILE=str(tmp_path / "missing.key"),
        OS3_BRIDGE_DOCTOR_SKIP_SMOKE="1",
        OS3_BRIDGE_DOCTOR_SKIP_CRED="1",
        OS3_BRIDGE_PORT="1",
    )
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["bash", str(root / "doctor.sh")], env=env, capture_output=True, text=True, timeout=120
    ).stdout
    assert "fallback chain: openrouter -> codex" in out
    env["OS3_BRIDGE_BACKEND"] = "codex"
    out = subprocess.run(
        ["bash", str(root / "doctor.sh")], env=env, capture_output=True, text=True, timeout=120
    ).stdout
    assert "fallback chain: codex -> openrouter" in out
    assert "WARNING: The chain moves from a subscription backend (codex)" in out


def test_a_cli_primary_hands_a_tools_request_to_the_chain(rig) -> None:
    # The CLI would answer 400 tools_not_supported; the next capable backend serves it.
    r = rig()
    response = r.chat(tools=TOOLS)
    assert response.status_code == 200
    assert "from nous" in response.text
    assert r.calls == {"codex": 0, "claude-code": 0, "nous": 1, "openrouter": 0}


def test_a_cli_primary_still_serves_plain_chat(rig) -> None:
    r = rig()
    assert "from codex" in r.chat().text
    assert r.calls["codex"] == 1
