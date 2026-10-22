"""One chat request sees one active backend, from the model remap through dispatch."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexus import app as app_module
from nexus.app import create_app
from nexus.backends.base import ChatUpstream
from nexus.config import BACKEND_DEFAULT_MODELS
from nexus.errors import GatewayError

UNKNOWN = "stealth/space-bunny-alpha"
NOUS_MODEL = BACKEND_DEFAULT_MODELS["nous"]
OPENROUTER_MODEL = BACKEND_DEFAULT_MODELS["openrouter"]


class Fake:
    def __init__(
        self,
        backend_id: str,
        *,
        listed: list[str] | None = None,
        fail: GatewayError | None = None,
        on_list: Any = None,
    ) -> None:
        self.id = backend_id
        self.listed = listed or []
        self.fail = fail
        self.on_list = on_list
        self.calls: list[dict[str, Any]] = []

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "rest",
            "key_present": True,
            "ready": True,
            "hint": "ok",
        }

    async def list_models(self) -> list[dict[str, Any]]:
        if self.on_list is not None:
            self.on_list()
        return [{"id": name} for name in self.listed]

    async def model_reachable(self, model: str) -> bool | None:
        return True

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        self.calls.append(dict(payload))
        if self.fail is not None:
            raise self.fail
        body = {
            "model": payload.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.id}}],
        }
        return ChatUpstream(200, "application/json", body=json.dumps(body).encode())


def _chat(client: TestClient, headers: dict[str, str]) -> Any:
    body = {"model": UNKNOWN, "messages": [{"role": "user", "content": "hi"}]}
    return client.post("/v1/chat/completions", headers=headers, json=body)


@pytest.fixture(autouse=True)
def remap_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OS3_BRIDGE_MODEL_FALLBACK")


def test_a_backend_switch_during_the_remap_wait_does_not_split_the_request(
    monkeypatch: pytest.MonkeyPatch, auth_header: dict[str, str]
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")

    def switch_to_openrouter() -> None:
        monkeypatch.setenv("OS3_BRIDGE_BACKEND", "openrouter")

    fakes = {
        "nous": Fake("nous", listed=["other/model"], on_list=switch_to_openrouter),
        "openrouter": Fake("openrouter", listed=["other/model"]),
    }
    monkeypatch.setattr(
        app_module, "build_backend", lambda: fakes[os.environ["OS3_BRIDGE_BACKEND"]]
    )
    with TestClient(create_app()) as client:
        response = _chat(client, auth_header)
    assert response.status_code == 200
    assert os.environ["OS3_BRIDGE_BACKEND"] == "openrouter"
    assert [call["model"] for call in fakes["nous"].calls] == [NOUS_MODEL]
    assert fakes["openrouter"].calls == []
    assert response.headers["x-os3-model-remapped"] == UNKNOWN


def test_a_remap_with_a_fallback_chain_gives_the_secondary_its_own_model(
    monkeypatch: pytest.MonkeyPatch, auth_header: dict[str, str]
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "nous")
    monkeypatch.setenv("OS3_BRIDGE_FALLBACK", "nous,openrouter")
    detail = {"backend": "nous", "upstream_status": 429, "upstream_body": ""}
    quota = GatewayError(
        502,
        "nous upstream error",
        "upstream_error",
        "nous_upstream_error",
        extra={"nous": detail, "upstream": detail},
    )
    nous = Fake("nous", listed=["other/model"], fail=quota)
    openrouter = Fake("openrouter")
    app = create_app(backend=nous)
    app.state.backend_factory = {"nous": nous, "openrouter": openrouter}.__getitem__
    with TestClient(app) as client:
        response = _chat(client, auth_header)
    assert response.status_code == 200
    assert response.headers["x-os3-backend"] == "openrouter"
    assert response.headers["x-os3-model-remapped"] == UNKNOWN
    assert [call["model"] for call in nous.calls] == [NOUS_MODEL]
    assert [call["model"] for call in openrouter.calls] == [OPENROUTER_MODEL]
    assert NOUS_MODEL != OPENROUTER_MODEL
