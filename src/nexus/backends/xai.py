"""xAI / Grok adapter. OpenAI-compatible API. Key never logged."""

from __future__ import annotations

import httpx

from nexus.backends.openai_compat import OpenAICompatibleBackend
from nexus.config import xai_base, xai_key_path

FALLBACK_MODELS = (
    "grok-4",
    "grok-3",
    "grok-3-mini",
)


class XAIBackend(OpenAICompatibleBackend):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            backend_id="xai",
            role="optional",
            label="xAI",
            owned_by="xai",
            env_var="XAI_API_KEY",
            key_path=xai_key_path,
            base_url=xai_base,
            fallback_models=FALLBACK_MODELS,
            transport=transport,
        )
