"""Doctor probe: /health, /v1/models, /v1/chat/completions, no secret text."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from nexus.app import create_app
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL
from nexus.probe import execute_probe, run_probe
from nexus.service import door_environment, render_installed_unit

from tests.transport import HandlerTransport

ROOT = Path(__file__).resolve().parent.parent


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


async def _against(app, bridge_key: str | None) -> tuple[int, str]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:11435",
        trust_env=False,
    ) as client:
        return await execute_probe(client, bridge_key=bridge_key)


def test_probe_contract_and_hides_secrets(
    bridge_key: str,
    openrouter_key: str,
) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": DEFAULT_MODEL,
                            "object": "model",
                            "created": 1,
                            "owned_by": "openrouter",
                        }
                    ]
                },
            )
        body = json.loads(request.content.decode())
        seen.append(body)
        return httpx.Response(200, json=_completion(f"ok {openrouter_key}"))

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    code, report = asyncio.run(_against(app, bridge_key))
    assert code == 0, report
    assert "health PASS" in report
    assert "backend=openrouter" in report
    assert "codex_required=false" in report
    assert "models PASS" in report
    assert "default_listed=true" in report
    assert "chat PASS" in report
    assert "content_present=true" in report
    assert seen[0]["model"] == DEFAULT_MODEL
    assert seen[0]["stream"] is False
    assert bridge_key not in report
    assert openrouter_key not in report
    assert "Bearer" not in report


def test_missing_bridge_key_does_not_mint_or_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "bridge.key"
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(key_path))

    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("probe must not call upstream without a bridge key")

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(explode)))
    code, report = asyncio.run(_against(app, None))
    assert code == 1
    assert "models FAIL bridge_key_missing" in report
    assert "chat FAIL bridge_key_missing" in report
    assert not key_path.exists()


def test_missing_upstream_key_is_a_code_not_a_secret(
    bridge_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = create_app()
    code, report = asyncio.run(_against(app, bridge_key))
    assert code == 1
    assert "models PASS" in report
    assert "chat FAIL" in report
    assert "code=openrouter_key_missing" in report
    assert bridge_key not in report
    assert "sk-" not in report


def test_skip_chat_does_not_post(
    bridge_key: str,
    openrouter_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del openrouter_key
    monkeypatch.setenv("OS3_BRIDGE_DOCTOR_SKIP_CHAT", "1")
    calls = {"chat": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            calls["chat"] += 1
            raise AssertionError("chat skipped")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": DEFAULT_MODEL,
                        "object": "model",
                        "created": 1,
                        "owned_by": "openrouter",
                    }
                ]
            },
        )

    app = create_app(backend=OpenRouterBackend(transport=HandlerTransport(handler)))
    code, report = asyncio.run(_against(app, bridge_key))
    assert code == 0, report
    assert "chat SKIP" in report
    assert calls["chat"] == 0


def test_unreachable_is_one_line() -> None:
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ConnectError("down")

    code, report = asyncio.run(run_probe(transport=Down(), base_url="http://bridge"))
    assert code == 1
    assert report == "health FAIL unreachable"


def test_connect_docs_and_launchagent_label() -> None:
    plist = ROOT / "packaging/launchd/ai.nexus.server.plist.template"
    text = plist.read_text(encoding="utf-8")
    assert "<string>ai.nexus.server</string>" in text
    assert "com.os3.bridge" not in text
    rendered = render_installed_unit(
        "launchd",
        repo=ROOT,
        python=ROOT / ".venv" / "bin" / "python",
        env=door_environment("openrouter"),
    )
    assert "<string>127.0.0.1</string>" in rendered
    assert "<string>11435</string>" in rendered
    assert not (ROOT / "packaging/launchd/com.os3.bridge.plist.template").exists()
    for name in ("README.md", "CONNECT.md"):
        page = (ROOT / name).read_text(encoding="utf-8")
        assert "http://127.0.0.1:11435/v1" in page
        assert "openai/gpt-4o-mini" in page
    assert DEFAULT_MODEL in (ROOT / "README.md").read_text(encoding="utf-8")
    assert DEFAULT_MODEL not in (ROOT / "CONNECT.md").read_text(encoding="utf-8")
    connect = (ROOT / "CONNECT.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ANTHROPIC_BASE_URL" in connect
    assert "ANTHROPIC_AUTH_TOKEN" in connect
    assert "/v1/messages" in connect
    assert "route returns 404" not in connect
    assert "your checkout" in connect
    assert "~/Projects/os3-bridge" not in connect
    backends = (ROOT / "docs" / "backends.md").read_text(encoding="utf-8")
    assert "/health" in backends
    assert "network I/O" in backends
    assert "cache" in backends.lower()
    assert "~/Projects/os3-bridge" not in readme
    assert "Rabbit is closed source" in connect
    assert "Screen Recording" in connect
    assert "OpenRouter credits" in connect
    script = (ROOT / "doctor.sh").read_text(encoding="utf-8")
    assert "nexus.probe" in script
    assert "Authorization" not in script
