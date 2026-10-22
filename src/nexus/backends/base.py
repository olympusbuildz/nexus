"""Backend protocol. Implementations must not log or return credential values."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ChatUpstream:
    status_code: int
    media_type: str
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None


class Backend(Protocol):
    id: str

    def describe(self) -> dict[str, Any]:
        """Readiness for /health and /doctor. Secret values are forbidden."""

    async def list_models(self) -> list[dict[str, Any]]:
        """OpenAI model objects: id, object, created, owned_by."""

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        """Forward one chat completion. Raise GatewayError when it cannot."""
