"""Provider API keys. Environment wins over the file. Values are never logged."""

from __future__ import annotations

import os
from pathlib import Path

from nexus.config import (
    anthropic_key_path,
    nous_key_path,
    openai_key_path,
    openrouter_key_path,
    xai_key_path,
)


def load_key(*, env_var: str, path: Path) -> str | None:
    env = os.environ.get(env_var, "").strip()
    if env:
        return env
    try:
        if not path.is_file():
            return None
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def key_source(*, env_var: str, path: Path) -> str | None:
    if os.environ.get(env_var, "").strip():
        return "env"
    try:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return "file"
    except OSError:
        return None
    return None


def configured_upstream_keys() -> list[str]:
    """Every provider key currently loaded. Used only to redact."""
    specs = (
        ("OPENROUTER_API_KEY", openrouter_key_path),
        ("OPENAI_API_KEY", openai_key_path),
        ("ANTHROPIC_API_KEY", anthropic_key_path),
        ("XAI_API_KEY", xai_key_path),
        ("NOUS_API_KEY", nous_key_path),
    )
    found: list[str] = []
    for env_var, path_fn in specs:
        key = load_key(env_var=env_var, path=path_fn())
        if key and key not in found:
            found.append(key)
    return found
