"""Usage and reset tracking: header parsing, the state file, /dashboard/usage, early switch."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import usage
from nexus.app import create_app
from nexus.dashboard_usage import _or_cache, usage_payload
from nexus.failover import USAGE_THRESHOLD, FailoverBackend, FailoverState

from tests.test_failover import Fake

NOW = 1_800_000_000.0
CODEX = {
    "x-codex-primary-used-percent": "42",
    "x-codex-primary-window-minutes": "300",
    "x-codex-primary-reset-after-seconds": "3600",
    "x-codex-primary-reset-at": str(int(NOW + 3600)),
    "x-codex-secondary-used-percent": "12.5",
    "x-codex-secondary-window-minutes": "10080",
    "x-codex-secondary-reset-at": str(int(NOW + 86400)),
    "x-codex-plan-type": "plus",
    "x-codex-turn-state": "OPAQUE-TURN-STATE-NEVER-STORED",
}
NOUS = {
    "x-ratelimit-limit-requests": "50",
    "x-ratelimit-remaining-requests": "40",
    "x-ratelimit-reset-requests": "59.999",
    "x-ratelimit-limit-tokens-1h": "6000000",
    "x-ratelimit-remaining-tokens-1h": "3000000",
    "x-ratelimit-reset-tokens-1h": "1m30s",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    usage._store = None
    _or_cache.clear()
    monkeypatch.delenv("NEXUS_USAGE_SWITCH_PERCENT", raising=False)


def test_codex_headers_parse_both_windows() -> None:
    parsed = usage.parse_codex_headers(CODEX, NOW)
    assert parsed is not None
    primary, secondary = parsed["windows"]
    assert primary["id"] == "primary" and primary["used_percent"] == 42.0
    assert primary["window_minutes"] == 300 and primary["reset_at"] == NOW + 3600
    assert secondary["used_percent"] == 12.5 and secondary["window_minutes"] == 10080
    assert parsed["plan"] == "plus"
    assert "OPAQUE" not in json.dumps(parsed)


def test_codex_reset_after_seconds_is_enough() -> None:
    headers = {"x-codex-primary-used-percent": "5", "x-codex-primary-reset-after-seconds": "60"}
    (window,) = usage.parse_codex_headers(headers, NOW)["windows"]
    assert window["reset_at"] == NOW + 60


def test_no_headers_means_no_reading() -> None:
    assert usage.parse_codex_headers({"content-type": "x"}, NOW) is None
    assert usage.parse_ratelimit_headers({"content-type": "x"}, NOW) is None


def test_ratelimit_headers_become_used_percent() -> None:
    parsed = usage.parse_ratelimit_headers(NOUS, NOW)
    assert parsed is not None
    by = {(w["used_percent"], w["limit"]) for w in parsed["windows"]}
    assert (20.0, 50.0) in by and (50.0, 6000000.0) in by
    resets = sorted(w["reset_at"] - NOW for w in parsed["windows"])
    assert resets == pytest.approx([60.0, 90.0], abs=1)


def test_level_thresholds() -> None:
    assert usage.level(None) == "unknown"
    assert usage.level(79.9) == "ok"
    assert usage.level(80) == "warn"
    assert usage.level(95) == "critical"


def test_switch_threshold_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert usage.switch_threshold() == 95.0
    monkeypatch.setenv("NEXUS_USAGE_SWITCH_PERCENT", "90")
    assert usage.switch_threshold() == 90.0
    monkeypatch.setenv("NEXUS_USAGE_SWITCH_PERCENT", "off")
    assert usage.switch_threshold() is None


def test_store_persists_atomically_and_privately(tmp_path) -> None:
    path = tmp_path / "u.json"
    clock = [NOW]
    s = usage.UsageStore(path, clock=lambda: clock[0])
    assert s.record_headers("chatgpt", CODEX)
    s.record_call("chatgpt", 120)
    s.record_call("nous", None)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert "OPAQUE" not in path.read_text()
    again = usage.UsageStore(path, clock=lambda: clock[0] + 10).snapshot()
    assert again["chatgpt"]["real"]["windows"][0]["resets_in_s"] == 3590
    assert again["chatgpt"]["estimate"]["requests_1h"] == 1
    assert again["chatgpt"]["estimate"]["tokens_5h"] == 120
    assert again["nous"]["estimate"]["requests_5h"] == 1


def test_store_ages_out_calls_and_marks_stale(tmp_path) -> None:
    clock = [NOW]
    s = usage.UsageStore(tmp_path / "u.json", clock=lambda: clock[0])
    s.record_headers("chatgpt", CODEX)
    s.record_call("chatgpt", 10)
    clock[0] += 2 * 3600
    snap = s.snapshot()["chatgpt"]
    assert snap["estimate"]["requests_1h"] == 0 and snap["estimate"]["requests_5h"] == 1
    assert snap["real"]["windows"][0]["stale"] is True
    assert snap["real"]["windows"][1]["stale"] is False


def test_corrupt_file_is_ignored(tmp_path) -> None:
    path = tmp_path / "u.json"
    path.write_text("{not json")
    s = usage.UsageStore(path)
    assert s.snapshot() == {}
    s.record_call("chatgpt", 1)
    assert json.loads(path.read_text())["backends"]["chatgpt"]["calls"]


def test_over_threshold_uses_live_real_windows_only(tmp_path) -> None:
    clock = [NOW]
    s = usage.UsageStore(tmp_path / "u.json", clock=lambda: clock[0])
    assert s.over_threshold("chatgpt", 95) is None
    s.record_headers("chatgpt", {**CODEX, "x-codex-primary-used-percent": "97"})
    assert s.over_threshold("chatgpt", 95) == (97.0, 3600.0)
    assert s.over_threshold("chatgpt", 98) is None
    clock[0] += 4000
    assert s.over_threshold("chatgpt", 95) is None


def test_dashboard_usage_reports_real_and_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "chatgpt")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "chatgpt,nous")
    usage.store().record_headers(
        "chatgpt", {**CODEX, "x-codex-primary-reset-at": str(int(time.time()) + 600)}
    )
    usage.store().record_call("nous", 33)
    client = TestClient(create_app(backend=Fake("chatgpt")))
    response = client.get("/dashboard/usage")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["threshold"] == 95.0 and body["auto_switch"]["enabled"] is True
    rows = {r["id"]: r for r in body["backends"]}
    assert [r["id"] for r in body["backends"]][:2] == ["chatgpt", "nous"]
    chatgpt = rows["chatgpt"]
    assert chatgpt["active"] and chatgpt["source"] == "real"
    assert chatgpt["windows"][0]["used_percent"] == 42.0
    assert 0 < chatgpt["windows"][0]["resets_in_s"] <= 600
    assert chatgpt["level"] == "ok"
    nous = rows["nous"]
    assert nous["source"] == "estimate" and nous["estimate"]["tokens_1h"] == 33
    assert "OPAQUE" not in response.text


def test_openrouter_key_usage_is_fetched_without_leaking_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    key = "sk-or-v1-" + "k" * 40
    key_file = tmp_path / "or.key"
    key_file.write_text(key)
    monkeypatch.setenv("NEXUS_OPENROUTER_KEY_FILE", str(key_file))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        assert request.url.path.endswith("/key")
        return httpx.Response(200, json={"data": {"usage": 2.5, "limit": 10, "label": key}})

    transport = httpx.MockTransport(handler)
    payload = asyncio.run(
        usage_payload(active="nous", failover=FailoverState(), transport=transport)
    )
    asyncio.run(usage_payload(active="nous", failover=FailoverState(), transport=transport))
    assert seen == [f"Bearer {key}"]
    rows = {r["id"]: r for r in payload["backends"]}
    assert rows["openrouter"]["account"]["used_percent"] == 25.0
    assert rows["openrouter"]["source"] == "real"
    assert key not in json.dumps(payload)


def test_openrouter_rejected_key_is_a_note(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    key_file = tmp_path / "or.key"
    key_file.write_text("sk-or-v1-" + "z" * 40)
    monkeypatch.setenv("NEXUS_OPENROUTER_KEY_FILE", str(key_file))
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    payload = asyncio.run(
        usage_payload(active="nous", failover=FailoverState(), transport=transport)
    )
    row = {r["id"]: r for r in payload["backends"]}["openrouter"]
    assert "401" in row["account"]["note"] and row["source"] == "estimate"


def _backend(primary: Fake, other: Fake, state: FailoverState) -> FailoverBackend:
    return FailoverBackend(primary, ["chatgpt", "nous"], state, 900.0, {"nous": other}.__getitem__)


def test_real_usage_at_threshold_switches_early() -> None:
    reset = int(time.time()) + 1800
    usage.store().record_headers(
        "chatgpt", {"x-codex-primary-used-percent": "96", "x-codex-primary-reset-at": str(reset)}
    )
    primary, other, state = Fake("chatgpt"), Fake("nous"), FailoverState()
    upstream = asyncio.run(_backend(primary, other, state).open_chat({"model": "m"}, stream=False))
    assert b"from nous" in upstream.body
    assert primary.calls == [] and len(other.calls) == 1
    (cool,) = _cooling(state)
    assert cool["backend"] == "chatgpt" and cool["reason"] == USAGE_THRESHOLD


def _cooling(state: FailoverState) -> list[dict]:
    from nexus.failover import fallback_config

    return state.snapshot(fallback_config(), "chatgpt")["cooling"]


def test_below_threshold_or_disabled_stays_on_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    reset = str(int(time.time()) + 1800)
    usage.store().record_headers(
        "chatgpt", {"x-codex-primary-used-percent": "94", "x-codex-primary-reset-at": reset}
    )
    primary, other, state = Fake("chatgpt"), Fake("nous"), FailoverState()
    asyncio.run(_backend(primary, other, state).open_chat({"model": "m"}, stream=False))
    assert len(primary.calls) == 1 and other.calls == []
    usage.store().record_headers(
        "chatgpt", {"x-codex-primary-used-percent": "99", "x-codex-primary-reset-at": reset}
    )
    monkeypatch.setenv("NEXUS_USAGE_SWITCH_PERCENT", "off")
    asyncio.run(_backend(primary, other, state).open_chat({"model": "m"}, stream=False))
    assert len(primary.calls) == 2 and other.calls == []


def test_early_switch_falls_back_to_primary_if_nothing_else_serves() -> None:
    reset = str(int(time.time()) + 1800)
    usage.store().record_headers(
        "chatgpt", {"x-codex-primary-used-percent": "99", "x-codex-primary-reset-at": reset}
    )
    primary, other = Fake("chatgpt"), Fake("nous", present=False)
    upstream = asyncio.run(
        _backend(primary, other, FailoverState()).open_chat({"model": "m"}, stream=False)
    )
    assert upstream.status_code == 200 and len(primary.calls) == 1


def test_pulse_counts_feed_the_estimate(monkeypatch: pytest.MonkeyPatch, bridge_key: str) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    client = TestClient(create_app(backend=Fake("nous")))
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bridge_key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    rows = {r["id"]: r for r in client.get("/dashboard/usage").json()["backends"]}
    assert rows["nous"]["estimate"]["requests_1h"] == 1
