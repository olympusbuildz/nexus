"""Active backend selection. OpenRouter is the default.

API-key backends: openrouter, openai, anthropic, xai.
ChatGPT plan over HTTP (Codex login): chatgpt.
Subscription CLIs: codex, claude-code, copilot, grok-build, antigravity.
`claude` stays the Anthropic API alias. `grok` stays the xAI API alias.
`claude-cli` is claude-code. `grok-cli` is grok-build. `agy` is antigravity.

POST /v1/messages is native for anthropic and translated for every other
working backend, including the subscription CLIs.
"""

from __future__ import annotations

from typing import Any

import httpx

from nexus.anthropic_messages import messages_route_status
from nexus.backends.anthropic import AnthropicBackend
from nexus.backends.base import Backend
from nexus.backends.chatgpt import ChatGPTBackend
from nexus.backends.nous import NousBackend
from nexus.backends.openai import OpenAIBackend
from nexus.backends.openrouter import OpenRouterBackend
from nexus.backends.subscription import (
    AntigravityBackend,
    ClaudeCodeBackend,
    CodexCliBackend,
    CopilotCliBackend,
    GrokBuildBackend,
)
from nexus.backends.xai import XAIBackend
from nexus.config import active_backend_id, canonical_backend
from nexus.errors import GatewayError

_API = {
    "openrouter": OpenRouterBackend,
    "openai": OpenAIBackend,
    "anthropic": AnthropicBackend,
    "xai": XAIBackend,
    "nous": NousBackend,
    "chatgpt": ChatGPTBackend,
}
_SUBSCRIPTION = {
    "codex": CodexCliBackend,
    "claude-code": ClaudeCodeBackend,
    "copilot": CopilotCliBackend,
    "grok-build": GrokBuildBackend,
    "antigravity": AntigravityBackend,
}
_KNOWN = (
    "openrouter, openai, anthropic (alias claude), xai (alias grok), "
    "nous, chatgpt (alias chatgpt-plan), codex, claude-code (alias claude-cli), "
    "copilot (alias github-copilot), "
    "grok-build (alias grok-cli), antigravity (alias agy)"
)


def active_backend_name() -> str:
    return active_backend_id()


def describe_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {name: cls().describe() for name, cls in _API.items()}
    for name, cls in _SUBSCRIPTION.items():
        catalog[name] = cls().describe()
    return catalog


def describe_routes() -> dict[str, dict[str, Any]]:
    return {"/v1/messages": messages_route_status(active_backend_name())}


def build_backend(
    name: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Backend:
    if name is None:
        chosen = active_backend_name()
    else:
        raw = name.strip().lower() or "openrouter"
        chosen = canonical_backend(raw)
    subscription = _SUBSCRIPTION.get(chosen)
    if subscription is not None:
        return subscription()
    api = _API.get(chosen)
    if api is not None:
        return api(transport=transport)
    raise GatewayError(
        400,
        f"Unknown backend '{chosen}'. Backends: {_KNOWN}.",
        "invalid_request_error",
        "unknown_backend",
    )
