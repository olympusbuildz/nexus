"""The ``state`` a UI shows for each backend row of /health.

A row already carries the raw signals (``key_present``, ``cli_present``, ``login``).
This turns them into one of five words and overlays the two facts only runtime
knows: the active backend's key was refused upstream, and a backend recently hit
a usage limit. Nothing here calls a provider or starts a CLI; the row came from
``describe_catalog`` and the verdict from ``model_readiness``.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from nexus.pulse import PulseLog

READY = "ready"
NOT_CONFIGURED = "not_configured"
CLI_MISSING = "cli_missing"
NOT_LOGGED_IN = "not_logged_in"
OUT_OF_QUOTA = "out_of_quota"

STATES = (READY, NOT_CONFIGURED, CLI_MISSING, NOT_LOGGED_IN, OUT_OF_QUOTA)


def kind_of(row: dict[str, Any]) -> str:
    return "cli" if row.get("kind") == "cli" else "rest"


def base_state(row: dict[str, Any]) -> str:
    """State from the row's own signals, with no runtime overlay."""
    if kind_of(row) == "cli":
        if not row.get("cli_present") or row.get("login") == "unchecked":
            return CLI_MISSING
        return READY if row.get("login") == "ok" else NOT_LOGGED_IN
    return READY if row.get("key_present") else NOT_CONFIGURED


def annotate_catalog(
    catalog: dict[str, dict[str, Any]],
    *,
    active: str,
    verdict: dict[str, Any] | None,
    pulse: PulseLog | None,
    cooling: Collection[str] = (),
) -> None:
    """Add ``state`` to every row in place, overlaying a refused key and a usage limit.

    ``ready`` and the existing ``hint`` stay what ``describe`` said unless the overlay
    changes the state; the overlay hint is the fix the UI shows verbatim.
    """
    for name, row in catalog.items():
        state = base_state(row)
        if (
            state == READY
            and name == active
            and verdict is not None
            and verdict.get("upstream_status") in {401, 403}
        ):
            state = NOT_CONFIGURED
            row["hint"] = str(verdict.get("hint") or row.get("hint"))
        if state == READY and (name in cooling or (pulse is not None and pulse.quota_hit(name))):
            state = OUT_OF_QUOTA
            label = row.get("name") or name
            row["hint"] = (
                f"Usage limit reached on {label}. Wait for the window to reset, or switch backend."
            )
        row["state"] = state
