"""GET /dashboard/config: what is configured, and where each setting came from.

Presence, source, path, and mode of every provider key, plus the last four
characters of a key as a recognisable mask. A key is never returned. The values
come from the same helpers /health and setup use, so this is one more view of
them, not a second source of truth.
"""

from __future__ import annotations

from typing import Any

from nexus.config import (
    active_backend_id,
    backend_choice_path,
    backend_source,
    default_model,
    model_choice_path,
    model_source,
    private_mode,
)
from nexus.keys import key_source, load_key
from nexus.setup_flow import API_BACKENDS

_MASK = "\u2022\u2022\u2022\u2022"
_MIN_MASKED_LENGTH = 20


def mask_key(key: str | None) -> str | None:
    """Bullets, plus the last four characters only when the key is long enough to spare them."""
    if not key:
        return None
    if len(key) >= _MIN_MASKED_LENGTH:
        return _MASK + key[-4:]
    return _MASK


def keys_payload() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for name, spec in API_BACKENDS.items():
        path = spec.key_path()
        key = load_key(env_var=spec.env_var, path=path)
        rows[name] = {
            "present": key is not None,
            "source": key_source(env_var=spec.env_var, path=path),
            "file": str(path),
            "mode": private_mode(path),
            "masked": mask_key(key),
        }
    return rows


def config_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "backend": active_backend_id(),
        "backend_source": backend_source(),
        "model": default_model(),
        "model_source": model_source(),
        "choice_file": str(backend_choice_path()),
        "model_file": str(model_choice_path()),
        "keys": keys_payload(),
    }
