"""OpenAI API adapter. OpenAI-compatible /v1 chat and models. Key never logged."""

from __future__ import annotations

import httpx

from nexus.backends.openai_compat import OpenAICompatibleBackend
from nexus.config import openai_base, openai_key_path

FALLBACK_MODELS = (
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1-mini",
)


class OpenAIBackend(OpenAICompatibleBackend):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            backend_id="openai",
            role="optional",
            label="OpenAI",
            owned_by="openai",
            env_var="OPENAI_API_KEY",
            key_path=openai_key_path,
            base_url=openai_base,
            fallback_models=FALLBACK_MODELS,
            transport=transport,
        )
