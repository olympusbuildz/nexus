"""Serve the active backend's model when a client names one it does not list.

A client such as OS3 may send a model id from another provider (an OpenRouter id
while Nous is active). The upstream would answer 404. When the backend's live
model list is known and the id is not in it, the request is sent to the model
the bridge already treats as the active one (``model_for``). Everything that
cannot be judged is left alone: no list, a CLI backend that resolves the model
itself, a request with no model.

``NEXUS_MODEL_FALLBACK=off`` restores pass-through.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from nexus.backends.base import Backend
from nexus.backends.subscription import SubscriptionBackend
from nexus.config import health_timeout, model_fallback_enabled, model_for
from nexus.pulse import safe_model

logger = logging.getLogger("nexus")

HEADER = "X-OS3-Model-Remapped"
_TTL_S = 60.0
_UNSAFE = "(unprintable id)"

Catalog = dict[str, tuple[float, tuple[str, ...] | None]]


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """The model to send, and the id the client asked for when that was replaced."""

    model: str
    remapped_from: str | None = None


async def _live_ids(backend: Backend, catalog: Catalog) -> tuple[str, ...] | None:
    """Ids the upstream confirmed. Rows carrying a ``warning`` are the bridge's own."""
    hit = catalog.get(backend.id)
    if hit is not None and time.monotonic() - hit[0] < _TTL_S:
        return hit[1]
    ids: tuple[str, ...] | None
    try:
        rows = await asyncio.wait_for(backend.list_models(), timeout=health_timeout())
        ids = tuple(
            row["id"] for row in rows if isinstance(row.get("id"), str) and "warning" not in row
        )
    except Exception:
        ids = None
    catalog[backend.id] = (time.monotonic(), ids or None)
    return ids or None


async def choose_model(backend: Backend, requested: str, catalog: Catalog) -> ModelChoice:
    """Resolve ``requested`` against the backend's live list."""
    if not model_fallback_enabled() or isinstance(backend, SubscriptionBackend):
        return ModelChoice(requested)
    ids = await _live_ids(backend, catalog)
    if ids is None:
        return ModelChoice(requested)
    wanted = requested.strip()
    if wanted in ids:
        return ModelChoice(wanted)
    folded = wanted.casefold()
    canonical = next((known for known in ids if known.casefold() == folded), None)
    if canonical is not None:
        return ModelChoice(canonical)
    served = model_for(backend.id)
    if served == requested:
        return ModelChoice(requested)
    shown = safe_model(requested) or _UNSAFE
    logger.info("model remapped backend=%s %s -> %s", backend.id, shown, served)
    return ModelChoice(served, shown)


def remap_headers(choice: ModelChoice) -> dict[str, str]:
    return {HEADER: choice.remapped_from} if choice.remapped_from else {}
