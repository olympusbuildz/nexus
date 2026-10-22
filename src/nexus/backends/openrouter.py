"""OpenRouter adapter. OpenAI-compatible /v1 pass-through. Key never logged."""

from __future__ import annotations

import httpx

from nexus.backends.openai_compat import OpenAICompatibleBackend
from nexus.config import door_origin, openrouter_base, openrouter_key_path
from nexus.keys import key_source, load_key

EXTRA_MODELS = (
    "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.3-70b-instruct",
)


def load_openrouter_key() -> str | None:
    return load_key(env_var="OPENROUTER_API_KEY", path=openrouter_key_path())


def openrouter_key_source() -> str | None:
    return key_source(env_var="OPENROUTER_API_KEY", path=openrouter_key_path())


class OpenRouterBackend(OpenAICompatibleBackend):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            backend_id="openrouter",
            role="primary",
            label="OpenRouter",
            owned_by="openrouter",
            env_var="OPENROUTER_API_KEY",
            key_path=openrouter_key_path,
            base_url=openrouter_base,
            fallback_models=EXTRA_MODELS,
            extra_headers={
                "HTTP-Referer": door_origin(),
                "X-Title": "nexus",
            },
            transport=transport,
            # OpenRouter's /models catalog answers 200 to any bearer; GET /key is
            # the cheap authenticated call that actually checks it.
            credential_path="/key",
        )
