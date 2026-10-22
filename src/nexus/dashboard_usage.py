"""GET /dashboard/usage: per-backend plan usage, reset times, and the early-switch state.

Rows come from ``nexus.usage`` (real provider numbers plus local estimates) and, for
OpenRouter with a key on file, one cached ``GET /api/v1/key`` call. No key, token or
header value outside the numeric allow-list ever reaches this payload.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from nexus import usage
from nexus.anthropic_messages import _LABELS
from nexus.config import openrouter_base
from nexus.failover import USAGE_THRESHOLD, FailoverState, effective_chain, fallback_config

_OR_TTL = 60.0
_or_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_PLAN_TTL = 60.0
_plan_cache: dict[str, tuple[float, str | None]] = {}


def _plan_poll_enabled() -> bool:
    from nexus.config import environ_get

    raw = (environ_get("NEXUS_CHATGPT_USAGE_POLL") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


async def _chatgpt_plan(transport: httpx.AsyncBaseTransport | None) -> str | None:
    """Refresh the ChatGPT plan windows from its usage endpoint. Cached 60 s.

    Only when a ChatGPT login is on file. Returns a note when the read failed; the
    last reading (with its age) stays on the row either way.
    """
    from nexus import chatgpt_auth
    from nexus.backends.chatgpt import fetch_plan_usage

    if not _plan_poll_enabled() or chatgpt_auth.login_status()[0] != "ok":
        return None
    cached = _plan_cache.get("chatgpt")
    if cached is not None and time.monotonic() - cached[0] < _PLAN_TTL:
        return cached[1]
    parsed, note = await fetch_plan_usage(transport)
    if parsed is not None:
        usage.store().record_real("chatgpt", parsed)
    _plan_cache["chatgpt"] = (time.monotonic(), note)
    return note


async def _openrouter_account(transport: httpx.AsyncBaseTransport | None) -> dict[str, Any] | None:
    """OpenRouter's own credit usage/limit for the key on file. Cached 60 s."""
    from nexus.backends.openrouter import load_openrouter_key

    key = load_openrouter_key()
    if key is None:
        return None
    cached = _or_cache.get("openrouter")
    if cached is not None and time.monotonic() - cached[0] < _OR_TTL:
        return cached[1]
    result: dict[str, Any] | None
    try:
        async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
            response = await client.get(
                f"{openrouter_base()}/key", headers={"Authorization": f"Bearer {key}"}
            )
    except httpx.HTTPError:
        result = {"note": "OpenRouter /key could not be reached."}
    else:
        if response.status_code in {401, 403}:
            result = {"note": f"OpenRouter refused the key (HTTP {response.status_code})."}
        elif response.status_code != 200:
            result = {"note": f"OpenRouter /key answered HTTP {response.status_code}."}
        else:
            try:
                data = (response.json() or {}).get("data") or {}
            except ValueError:
                data = {}
            result = {
                k: data.get(k)
                for k in ("usage", "limit", "limit_remaining", "is_free_tier", "usage_daily")
                if isinstance(data.get(k), int | float | bool) or data.get(k) is None
            }
            result = {k: v for k, v in result.items() if v is not None}
            limit, used = result.get("limit"), result.get("usage")
            if isinstance(limit, int | float) and limit > 0 and isinstance(used, int | float):
                result["used_percent"] = round(min(100.0, used / limit * 100), 2)
    _or_cache["openrouter"] = (time.monotonic(), result)
    if result is not None:
        usage.store().record_account("openrouter", result)
    return result


def _row(name: str, raw: dict[str, Any], *, active: str, cooling: dict[str, Any]) -> dict[str, Any]:
    real = raw.get("real") if isinstance(raw.get("real"), dict) else None
    windows = []
    for window in (real or {}).get("windows") or []:
        level = "unknown" if window.get("stale") else usage.level(window["used_percent"])
        windows.append({**window, "source": "real", "level": level})
    live = [w["used_percent"] for w in windows if not w.get("stale")]
    peak = max(live) if live else None
    account = raw.get("account") if isinstance(raw.get("account"), dict) else None
    if account and isinstance(account.get("used_percent"), int | float):
        peak = max(peak or 0.0, float(account["used_percent"]))
    row: dict[str, Any] = {
        "id": name,
        "label": _LABELS.get(name, name),
        "active": name == active,
        "windows": windows,
        "estimate": raw.get("estimate") or {},
        "source": "real" if windows or (account and "used_percent" in account) else "estimate",
        "used_percent": peak,
        "level": usage.level(peak),
        "updated_at": (real or {}).get("updated_at"),
        "from": (real or {}).get("from"),
        "age_s": (real or {}).get("age_s"),
    }
    if "images_share_plan" in raw:
        row["images_share_plan"] = bool(raw["images_share_plan"])
    for key in ("plan", "credits", "limit_reached"):
        if real and key in real:
            row[key] = real[key]
    if account:
        row["account"] = account
    if name in cooling:
        row["cooling"] = cooling[name]
    return row


async def usage_payload(
    *, active: str, failover: FailoverState, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    await _openrouter_account(transport)
    plan_note = await _chatgpt_plan(transport)
    snapshot = usage.store().snapshot()
    config = fallback_config()
    fallback = failover.snapshot(config, active)
    cooling = {c["backend"]: c for c in fallback["cooling"]}
    names = [active, *[n for n in effective_chain(config, active) if n != active]]
    names += sorted(n for n in snapshot if n not in names)
    rows = [_row(n, snapshot.get(n, {}), active=active, cooling=cooling) for n in names]
    for row in rows:
        if row["id"] == "chatgpt" and plan_note:
            row["note"] = plan_note
    threshold = usage.switch_threshold()
    return {
        "ok": True,
        "now": int(time.time()),
        "threshold": threshold,
        "warn_percent": usage.WARN_PERCENT,
        "critical_percent": usage.CRITICAL_PERCENT,
        "auto_switch": {
            "enabled": threshold is not None and fallback["enabled"],
            "chain": fallback["chain"],
            "serving": fallback["active"],
            "benched": [c for c in fallback["cooling"] if c["reason"] == USAGE_THRESHOLD],
        },
        "backends": rows,
    }
