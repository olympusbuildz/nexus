"""GET /dashboard/models: where each id in the model picker came from.

The picker must not present a catalog as an entitlement. Every id here carries
a status: ``live`` (the provider listed it for this key), ``cli`` (the CLI
reported it), ``catalog`` (the bridge's own starting guess), or ``unverified``
(the bridge's default that the live list did not contain).

An API-key backend with a key on file is asked for its real list, within the
same budget /health uses. Nothing else is fetched, and the key is never part of
the payload. Each row names the backend whose list it came from.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.backends.registry import build_backend
from nexus.config import canonical_backend, health_timeout
from nexus.errors import GatewayError
from nexus.redact import redact

MAX_IDS = 400


def _entry(model_id: str, status: str, backend: str) -> dict[str, str]:
    return {"id": model_id, "status": status, "backend": backend}


def _clean(ids: list[tuple[str, str]], backend: str) -> list[dict[str, str]]:
    """Unique, non-empty, non-secret-shaped ids, in the order given."""
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for model_id, status in ids:
        if not model_id or model_id in seen or "[REDACTED" in model_id:
            continue
        if redact(model_id) != model_id:
            continue
        seen.add(model_id)
        rows.append(_entry(model_id, status, backend))
    return rows


async def _live_rows(backend: Any) -> list[dict[str, Any]] | None:
    try:
        rows = await asyncio.wait_for(backend.list_models(), timeout=health_timeout())
    except Exception:
        return None
    if not any(isinstance(row, dict) and "warning" not in row for row in rows):
        return None
    return rows


async def models_payload(name: str) -> dict[str, Any] | None:
    """The picker payload for one backend, or None when the name is not one we implement."""
    try:
        backend = build_backend(canonical_backend(name))
    except GatewayError:
        return None
    row = await asyncio.to_thread(backend.describe)
    default = str(row.get("default_model") or "")
    is_cli = row.get("kind") == "cli"

    if not is_cli and row.get("key_present"):
        live = await _live_rows(backend)
        if live is not None:
            picked = [
                (
                    str(item.get("id") or ""),
                    "unverified" if "warning" in item else "live",
                )
                for item in live
                if isinstance(item, dict)
            ]
            models = _clean(picked, backend.id)
            return {
                "backend": backend.id,
                "source": "live",
                "hint": (
                    "Listed by the provider for this key. An id marked unverified is the "
                    "Nexus default and was not in that list."
                ),
                "default_model": default,
                "models": models[:MAX_IDS],
                "truncated": len(models) > MAX_IDS,
            }

    source = "cli" if row.get("models_source") == "cli" else "catalog"
    ids = [(str(item), source) for item in row.get("models") or []]
    if default and default not in {model_id for model_id, _ in ids}:
        ids.insert(0, (default, "catalog"))
    return {
        "backend": backend.id,
        "source": source,
        "hint": str(row.get("models_hint") or ""),
        "default_model": default,
        "models": _clean(ids, backend.id),
        "truncated": False,
    }
