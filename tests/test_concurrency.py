"""In-process inflight cap. Default allows overlap; the cap still serializes."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL

from tests.transport import HandlerTransport

COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1,
    "model": DEFAULT_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
}


async def _overlap(limit: int, bridge_key: str) -> int:
    current = 0
    peak = 0
    release = asyncio.Event()
    first = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        if current == 1:
            first.set()
        await release.wait()
        current -= 1
        return httpx.Response(200, json=COMPLETION)

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:

        async def once() -> httpx.Response:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {bridge_key}"},
                json={"messages": [{"role": "user", "content": "hi"}]},
            )

        first_task = asyncio.create_task(once())
        second_task = asyncio.create_task(once())
        await asyncio.wait_for(first.wait(), timeout=2)
        await asyncio.sleep(0.2)
        observed = peak
        release.set()
        done = await asyncio.gather(first_task, second_task)
    assert observed == min(2, limit)
    assert all(item.status_code == 200 for item in done)
    return peak


def test_inflight_cap_serializes_and_default_overlaps(
    bridge_key: str,
    openrouter_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del openrouter_key
    monkeypatch.setenv("OS3_BRIDGE_MAX_INFLIGHT", "1")
    assert asyncio.run(_overlap(1, bridge_key)) == 1
    monkeypatch.setenv("OS3_BRIDGE_MAX_INFLIGHT", "2")
    assert asyncio.run(_overlap(2, bridge_key)) == 2
