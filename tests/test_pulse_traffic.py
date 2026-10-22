"""Pulse traffic totals stay honest: process-local, no invented plan quotas."""

from __future__ import annotations

from nexus.pulse import PulseLog


def test_traffic_starts_empty_and_names_the_non_claim() -> None:
    pulse = PulseLog(cap=4)
    traffic = pulse.traffic()
    assert traffic["calls"] == 0
    assert traffic["ok"] == 0
    assert traffic["tokens"]["total"] is None
    assert traffic["tokens"]["known_calls"] == 0
    assert traffic["latency_ms"]["p50"] is None
    assert "plan balance" in traffic["note"].lower()


def test_traffic_counts_and_keeps_unknown_tokens_blank() -> None:
    pulse = PulseLog(cap=10)
    pulse.record(method="POST", path="/v1/chat/completions", model="m", status=200, latency_ms=10)
    pulse.record(method="POST", path="/v1/chat/completions", model="m", status=429, latency_ms=20)
    pulse.record(
        method="POST",
        path="/v1/chat/completions",
        model="m",
        status=200,
        latency_ms=30,
        prompt_tokens=5,
        completion_tokens=2,
    )
    traffic = pulse.traffic()
    assert traffic["calls"] == 3
    assert traffic["ok"] == 2
    assert traffic["client_error"] == 1
    assert traffic["usage_limit"] == 1
    assert traffic["tokens"]["prompt"] == 5
    assert traffic["tokens"]["completion"] == 2
    assert traffic["tokens"]["total"] == 7
    assert traffic["tokens"]["known_calls"] == 1
    assert traffic["tokens"]["unknown_calls"] == 2
    assert traffic["latency_ms"]["sample"] == 3
    assert traffic["latency_ms"]["max"] == 30
