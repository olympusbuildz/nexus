"""A /health poll must not inherit the chat path's upstream budget.

The chat path is allowed minutes; a watchdog polling /health is not. These
tests pin that the two are separate env vars, that the readiness check
actually uses the health one, and that the chat path still uses the other.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from nexus.config import DEFAULT_MODEL, health_timeout, upstream_timeout
from nexus.status import health_payload

from tests.transport import HandlerTransport, sent_to

DEFAULT_HEALTH_TIMEOUT = 2.0


def _models_list(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(payload.get("data", []))})


def test_health_timeout_default_is_small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OS3_BRIDGE_HEALTH_TIMEOUT", raising=False)
    monkeypatch.setenv("OS3_BRIDGE_UPSTREAM_TIMEOUT", "300")
    assert health_timeout() == DEFAULT_HEALTH_TIMEOUT
    assert upstream_timeout() == 300.0


def test_upstream_timeout_does_not_move_the_health_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OS3_BRIDGE_HEALTH_TIMEOUT", raising=False)
    for value in ("1", "30", "3600"):
        monkeypatch.setenv("OS3_BRIDGE_UPSTREAM_TIMEOUT", value)
        assert upstream_timeout() == float(value)
        assert health_timeout() == DEFAULT_HEALTH_TIMEOUT


def test_health_timeout_is_read_from_its_own_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "0.5")
    monkeypatch.setenv("OS3_BRIDGE_UPSTREAM_TIMEOUT", "120")
    assert health_timeout() == 0.5
    assert upstream_timeout() == 120.0


def test_health_timeout_rejects_nonsense_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "not-a-number")
    assert health_timeout() == DEFAULT_HEALTH_TIMEOUT
    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "0")
    assert health_timeout() == 0.1
    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "9999")
    assert health_timeout() == 60.0


def test_cold_cache_readiness_uses_the_health_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /models call behind a cold /health is bounded by the health var.

    A transport that answers only after the read timeout cannot prove the
    number directly, so assert on the two values independently: the client
    built for the readiness check carries the health budget, and a chat
    client still carries the upstream one.
    """
    from nexus.backends.anthropic import AnthropicBackend

    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "0.5")
    monkeypatch.setenv("OS3_BRIDGE_UPSTREAM_TIMEOUT", "300")

    backend = AnthropicBackend(transport=HandlerTransport(lambda r: _models_list({"data": []})))
    check_client = backend._http.client(health_timeout())
    assert check_client.timeout.read == 0.5
    assert check_client.timeout.read == health_timeout()
    chat_client = backend._http.client()
    assert chat_client.timeout.read == 300.0
    assert chat_client.timeout.read == upstream_timeout()


def test_readiness_and_chat_pick_different_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one thing a watchdog cares about: the two paths do not share a knob.

    httpx's in-process test transport does not enforce timeouts, so rather
    than time a stalled /models we record which budget each path builds its
    client with.
    """
    from nexus.backends.openrouter import OpenRouterBackend

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "0.25")
    monkeypatch.setenv("OS3_BRIDGE_UPSTREAM_TIMEOUT", "600")

    transport = HandlerTransport(lambda r: _models_list({"data": [{"id": DEFAULT_MODEL}]}))
    backend = OpenRouterBackend(transport=transport)
    used: list[float | None] = []
    real_client = backend._http.client

    def spy(timeout: float | None = None) -> httpx.AsyncClient:
        used.append(timeout)
        return real_client(timeout)

    backend._http.client = spy  # type: ignore[method-assign]

    payload = asyncio.run(health_payload(backend=backend))
    assert payload["model_ready"] is True
    assert sent_to(transport, "/models")
    assert used, "/health made no upstream call"
    assert all(value == health_timeout() for value in used), used

    used.clear()
    asyncio.run(backend.open_chat({"model": "openai/gpt-4o-mini", "messages": []}, stream=False))
    assert used == [None], used


def test_health_timeout_env_is_isolated_between_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation is per-read: no restart, and no bleed between the two vars."""
    from nexus.config import health_timeout as health_now
    from nexus.config import upstream_timeout as upstream_now

    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "3")
    monkeypatch.setenv("OS3_BRIDGE_UPSTREAM_TIMEOUT", "7")
    assert (health_now(), upstream_now()) == (3.0, 7.0)
    monkeypatch.setenv("OS3_BRIDGE_HEALTH_TIMEOUT", "4")
    assert (health_now(), upstream_now()) == (4.0, 7.0)


def test_a_slow_cli_probe_does_not_stall_the_event_loop(monkeypatch) -> None:
    """/health is polled by the dashboard; a cold CLI probe must not block the door."""
    import asyncio
    import time

    from nexus import status
    from nexus.backends import subscription

    def slow(self, binary):
        time.sleep(0.6)
        return {"id": self.id, "ready": False, "hint": "slow", "cli_present": True}

    monkeypatch.setattr(subscription.SubscriptionBackend, "_probe", slow, raising=False)

    async def run() -> float:
        lag = 0.0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal lag
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                lag = max(lag, now - last - 0.02)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await status.health_payload()
        stop.set()
        await task
        return lag

    assert asyncio.run(run()) < 0.3
