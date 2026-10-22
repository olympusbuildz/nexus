"""Strip credential material from bytes we might return to a client."""

from __future__ import annotations

import re

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.~+/]{8,}={0,3}")
_KEYISH = re.compile(
    r"\b(?:sk-or-|sk-ant-|sk-proj-|xai-)[A-Za-z0-9_\-]{6,}\b",
)
_QUERY_SECRET = re.compile(
    r"(?i)(api_key|access_token|apikey|key)=([^&\s\"']+)",
)


def redact(text: str, secrets: list[str] | None = None) -> str:
    if not text:
        return text
    ordered = sorted(
        {secret for secret in (secrets or []) if secret and len(secret) >= 8},
        key=len,
        reverse=True,
    )
    for secret in ordered:
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KEYISH.sub("[REDACTED]", text)
    text = _QUERY_SECRET.sub(r"\1=[REDACTED]", text)
    return text


def redact_bytes(data: bytes, secrets: list[str] | None = None) -> bytes:
    """Redact a complete UTF-8 buffer. Leave invalid UTF-8 untouched.

    Callers that split a stream into chunks can miss a secret that crosses a
    chunk boundary; non-stream bodies are redacted as a whole.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    cleaned = redact(text, secrets)
    if cleaned == text:
        return data
    return cleaned.encode("utf-8")


def known_secrets() -> list[str]:
    """Secrets currently configured. Never includes values that are not loaded."""
    from nexus.auth import read_bridge_key
    from nexus.keys import configured_upstream_keys

    found: list[str] = []
    bridge = read_bridge_key()
    if bridge:
        found.append(bridge)
    for upstream in configured_upstream_keys():
        if upstream not in found:
            found.append(upstream)
    return found
