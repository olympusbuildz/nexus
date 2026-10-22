"""How a /v1/models row admits that nobody confirmed it.

The bridge lists a default model even when it cannot reach the upstream list
to check it. That keeps default-resolution working for clients that read the
list and pick the first row, but it must not look like a live model: a row
the upstream never confirmed carries a ``warning`` beside it. A client that
prints or forwards the row sees the caveat; a client that only reads ``id``
keeps working.
"""

from __future__ import annotations

from typing import Any

NOT_LISTED = (
    "the bridge's configured default model; it is not in the account's live "
    "model list, so chat requests naming it will fail. Set NEXUS_MODEL "
    "to a model this backend actually offers."
)
NO_UPSTREAM = (
    "the upstream model list could not be read, so this row is the bridge's "
    "own catalog and not a live model. Check ./doctor.sh, then retry."
)


def unverified(
    model_id: str,
    now: int,
    *,
    owned_by: str = "nexus",
    reason: str = NOT_LISTED,
) -> dict[str, Any]:
    """A model row the bridge could not confirm, with the caveat attached."""
    return {
        "id": model_id,
        "object": "model",
        "created": now,
        "owned_by": owned_by,
        "warning": f"unverified: {reason}",
    }


def first_warning(rows: list[dict[str, Any]]) -> str | None:
    """The first row's warning, or None when the leading row is confirmed."""
    if not rows:
        return None
    warning = rows[0].get("warning")
    return warning if isinstance(warning, str) else None


def billing(model_id: str) -> tuple[str, str | None]:
    """``("free", None)`` for an id ending ``:free``, else ``("unknown", warning)``.

    The bridge has no price list, so anything not marked free is unknown, not paid.
    """
    if model_id.lower().endswith(":free"):
        return "free", None
    return (
        "unknown",
        f"{model_id} is not marked free (no ':free' suffix); it may be billed by the provider "
        "or count against your plan.",
    )
