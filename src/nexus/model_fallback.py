"""Serve the active backend's model when a client names one it does not list.

A client such as OS3 may send a model id from another provider (an OpenRouter id
while Nous is active, or a ``vendor/model`` id while a subscription CLI is
active). The upstream would answer 404, and a CLI rejects anything that is not
a single model token. When that id can be judged foreign, the request is sent
to the model the bridge already treats as the active one (``model_for``).
Everything that cannot be judged is left alone: no list, a CLI token, a
request with no model.

``NEXUS_MODEL_FALLBACK=off`` restores pass-through.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from nexus.backends.base import Backend
from nexus.backends.subscription import _MODEL_OK, SubscriptionBackend
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


def _subscription_choice(backend: Backend, requested: str) -> ModelChoice:
    """A CLI token is the client's. A foreign id is the active model, not a 400.

    ``_MODEL_OK`` is the token rule ``_require_model`` enforces. A ``vendor/model``
    id fails it, which is how another provider's id arrives. An empty ``model_for``
    is left alone so that rejection still stands.
    """
    if _MODEL_OK.fullmatch(requested.strip()) is not None:
        return ModelChoice(requested)
    served = model_for(backend.id)
    if not served or served == requested:
        return ModelChoice(requested)
    shown = safe_model(requested) or _UNSAFE
    logger.info("model remapped backend=%s %s -> %s", backend.id, shown, served)
    return ModelChoice(served, shown)


async def choose_model(backend: Backend, requested: str, catalog: Catalog) -> ModelChoice:
    """Resolve ``requested`` against the backend's live list."""
    if not model_fallback_enabled():
        return ModelChoice(requested)
    if isinstance(backend, SubscriptionBackend):
        return _subscription_choice(backend, requested)
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
