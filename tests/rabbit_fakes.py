"""Fakes for the Rabbit OS3 setup tests. No Chrome, no websocket, no Rabbit."""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from nexus.rabbit_cdp import ApiResponse, ManualRequired

FIXTURES = Path(__file__).parent / "fixtures" / "rabbit"
BRIDGE_KEY = "os3_FIXTURE_bridge_key_c1d7"
RESTORE_KEY = "sk-or-v1-FIXTURE-restore-key-wxyz"
NODE_ID = "node-fixture-1"
ENDPOINT = "http://127.0.0.1:11435/v1"
MODELS = [
    {"id": "meituan/longcat-2.5-preview:free", "object": "model"},
    {"id": "openai/gpt-4o-mini", "object": "model"},
    {"id": "Google/Gemma-4-31B-it:free", "object": "model"},
    {"id": "anthropic/claude-sonnet-4.5", "object": "model"},
    {"id": "llama-3.1-8b", "object": "model"},
]


STEALTH_MODEL = "stealth/space-bunny-alpha"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def models_source() -> list[dict[str, Any]]:
    return copy.deepcopy(MODELS)


class FakeOs:
    """The server side of OS3's BYOK endpoints, as one signed-in page sees them."""

    def __init__(self, initial: Any = None) -> None:
        self.state: dict[str, Any] | None = None
        if initial is not None:
            self.state = copy.deepcopy(initial.get("credential", initial))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.registered_keys: list[str] = []
        self.register_status = 200
        self.delete_status = 200
        self.delete_needs_key = False
        self.delete_keeps_credential = False
        self.update_model_status = 200
        self.fail_gets_after_delete = 0
        self.get_error: Exception | None = None
        self._gets_to_fail = 0
        self.fail_register_times = 0
        self.get_status = 200
        self.signed_out = False
        self.blocked = False
        self.nested = True
        # Like real OS3: a Local register is refused unless the model is in the endpoint's list.
        self.endpoint_models: dict[str, list[str]] = {ENDPOINT: [m["id"] for m in MODELS]}

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def _raise_if_unreachable(self) -> None:
        if self.signed_out:
            raise ManualRequired("signed_out", "No OS3 session token.")
        if self.blocked:
            raise ManualRequired("blocked", "CloudFront 403.")

    async def call(self, path: str, body: dict[str, Any]) -> ApiResponse:
        self._raise_if_unreachable()
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/api/byok/get":
            if self._gets_to_fail > 0:
                self._gets_to_fail -= 1
                if self.get_error is not None:
                    raise self.get_error
                return ApiResponse(503, None)
            if self.get_status != 200:
                return ApiResponse(self.get_status, None)
            data = {"credential": self.state} if self.nested else (self.state or {})
            return ApiResponse(200, copy.deepcopy(data))
        if path == "/api/byok/delete":
            if self.delete_needs_key and not body.get("key"):
                return ApiResponse(400, {"error": "key required"})
            if self.delete_status == 200:
                self._gets_to_fail = self.fail_gets_after_delete
            if self.delete_status == 200 and not self.delete_keeps_credential:
                self.state = None
            return ApiResponse(self.delete_status, {"ok": self.delete_status == 200})
        if path == "/api/byok/update-model":
            # Real shape: one call per tier, no provider and no tierModels map.
            if not (body.get("tier") in ("small", "standard") and body.get("model")):
                return ApiResponse(400, {"error": "tier and model are required"})
            if self.update_model_status != 200 or self.state is None:
                return ApiResponse(self.update_model_status, {"error": "refused"})
            tiers = dict(self.state.get("tierModels") or {})
            tiers[body["tier"]] = body["model"]
            self.state["tierModels"] = tiers
            return ApiResponse(200, {"ok": True, "credential": copy.deepcopy(self.state)})
        return ApiResponse(404, None)

    async def register(self, body: dict[str, Any], key: str) -> ApiResponse:
        self._raise_if_unreachable()
        self.calls.append(("/api/byok/register", copy.deepcopy(body)))
        self.registered_keys.append(key)
        if self.fail_register_times > 0:
            self.fail_register_times -= 1
            return ApiResponse(500, {"error": "boom"})
        if self.register_status != 200:
            return ApiResponse(self.register_status, {"error": "refused"})
        if body["provider"] not in ("local", "custom"):
            return ApiResponse(400, {"error": "unknown provider"})
        if body["provider"] == "custom" and not body.get("endpoint"):
            return ApiResponse(400, {"error": "endpoint is required for a custom provider"})
        listed = self.endpoint_models.get(str(body.get("endpoint")))
        if body["provider"] == "local" and listed is not None and body.get("modelId") not in listed:
            return ApiResponse(400, {"error": "model not served by the endpoint"})
        self.state = {
            "provider": body["provider"],
            "maskedKey": "****" + key[-4:],
            "model": body.get("modelId"),
            "tierModels": None,
            "config": {
                "endpoint": body.get("endpoint"),
                "modelId": body.get("modelId"),
                "nodeId": body.get("nodeId"),
                "contextWindow": body.get("contextWindow"),
            },
            "status": {"lastCallOutcome": None, "lastCallAt": None},
        }
        return ApiResponse(200, {"ok": True})


class FakeBrowser:
    """Stands in for ChromeCdp: ``open_page`` yields the fake OS page."""

    def __init__(self, os_page: FakeOs, *, unavailable: ManualRequired | None = None) -> None:
        self.page = os_page
        self.unavailable = unavailable
        self.opened = 0
        self.closed = 0

    def describe(self) -> dict[str, Any]:
        return {
            "host": "127.0.0.1",
            "port": 9333,
            "profile_dir": "/tmp/profile",
            "profile_exists": True,
            "chrome_found": True,
        }

    async def listening(self) -> bool:
        return self.unavailable is None

    @contextlib.asynccontextmanager
    async def open_page(self) -> AsyncIterator[FakeOs]:
        if self.unavailable is not None:
            raise self.unavailable
        self.opened += 1
        try:
            yield self.page
        finally:
            self.closed += 1
