"""/health and /doctor bodies. Shared with doctor.sh so the shell does not drift."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from nexus import __version__
from nexus.auth import read_bridge_key
from nexus.backend_state import annotate_catalog
from nexus.backends.registry import (
    active_backend_name,
    build_backend,
    describe_catalog,
    describe_routes,
)
from nexus.config import (
    anthropic_key_path,
    backend_choice_path,
    backend_source,
    bridge_key_path,
    configured_host,
    configured_port,
    model_for,
    nous_key_path,
    openai_key_path,
    openrouter_key_path,
    private_mode,
    rabbit_watchdog_enabled,
    xai_key_path,
)
from nexus.errors import OUT_OF_QUOTA, GatewayError
from nexus.failover import FailoverState, fallback_config
from nexus.pulse import PulseLog
from nexus.rabbit_watchdog import WATCHDOG
from nexus.roles import RoleModel, role_config

UI_API_VERSION = 1

ENDPOINTS = [
    "/health",
    "/doctor",
    "/v1/models",
    "/v1/chat/completions",
    "/v1/messages",
]


_MODEL_TTL = 10.0
_model_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _credential_verdict(backend: Any) -> dict[str, Any] | None:
    """Why the configured key is not known good, or None when nothing is wrong with it.

    Shared by every role's model check so one /health asks the provider once.
    """
    check = getattr(backend, "credential_check", None)
    if not callable(check):
        return None
    cache_key = f"credential:{backend.id}"
    cached = _model_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < _MODEL_TTL:
        return dict(cached[1]) or None
    try:
        verdict = await check()
    except Exception:
        verdict = None
    _model_cache[cache_key] = (time.monotonic(), dict(verdict or {}))
    return verdict


def _finish(
    out: dict[str, Any], verdict: dict[str, Any], unconfirmed: str, cache_key: str
) -> dict[str, Any]:
    out.update({"hint": unconfirmed, **verdict})
    _model_cache[cache_key] = (time.monotonic(), out)
    return out


async def model_readiness(
    backend: Any, model: str | None = None, setting: str = "NEXUS_MODEL"
) -> dict[str, Any]:
    """Can the active backend actually serve the model the bridge will send?

    ``model`` defaults to the bridge default; a role override passes its own id and
    the name of the setting to edit, so the hint points at the right place.

    "The key is present" is not readiness. A stranger with the default model
    id has a key, a listening door, and a /health that says ok. The only thing
    that separates them is whether the model id is live upstream, so ask.
    """
    name = backend.id
    model = model or model_for(name)
    cache_key = f"{name}:{model}:{setting}"
    cached = _model_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < _MODEL_TTL:
        return dict(cached[1])
    out: dict[str, Any] = {"backend": name, "model": model, "ready": None, "hint": "unchecked"}
    describe = await asyncio.to_thread(backend.describe)
    if describe.get("ready") is not True:
        out["hint"] = str(describe.get("hint") or "backend is not ready")
        _model_cache[cache_key] = (time.monotonic(), out)
        return out
    unconfirmed = (
        f"could not confirm model '{model}' with {name}; "
        f"set {setting} to a model your account can reach"
    )
    # "A key file exists" is not "the key works": some providers serve their model
    # catalog to any bearer, so those are asked a credential-bound question first.
    if getattr(backend, "credential_check_first", False):
        verdict = await _credential_verdict(backend)
        if verdict is not None:
            return _finish(out, verdict, unconfirmed, cache_key)
    try:
        reachable = await backend.model_reachable(model)
    except Exception:
        reachable = None
    if reachable is None and not getattr(backend, "credential_check_first", False):
        verdict = await _credential_verdict(backend)
        if verdict is not None:
            return _finish(out, verdict, unconfirmed, cache_key)
    out["ready"] = reachable
    if reachable:
        out["hint"] = "ok"
    elif reachable is None:
        out["hint"] = unconfirmed
    else:
        out["hint"] = (
            f"model '{model}' is not available on {name}. The bridge would send it and "
            f"every request would fail. Set {setting} to a model {name} offers "
            "(GET /v1/models) and re-check with ./doctor.sh"
        )
    _model_cache[cache_key] = (time.monotonic(), out)
    return out


async def roles_status(backend: Any | None) -> dict[str, Any]:
    """Per-role effective model and reachability, for the roles that name a model.

    A role with no override is listed with ``model: null``: OS3's own model field
    decides, and that is checked per request, not here. A role whose override cannot
    be confirmed on the active backend is not ready.
    """
    config = role_config()
    first: dict[str, RoleModel] = {}
    for item in config.roles.values():
        if item.model is not None:
            first.setdefault(item.model, item)
    results: dict[str, dict[str, Any]] = {}
    if backend is not None and first:
        answers = await asyncio.gather(
            *(model_readiness(backend, m, i.setting or "NEXUS_MODEL") for m, i in first.items())
        )
        results = dict(zip(first, answers, strict=True))
    roles: dict[str, dict[str, Any]] = {}
    for role, item in config.roles.items():
        entry: dict[str, Any] = {"model": item.model, "source": item.source}
        if item.model is not None:
            found = results.get(item.model)
            entry["ready"] = found["ready"] if found else False
            entry["hint"] = found["hint"] if found else "the backend is not known"
        elif item.problem:
            entry["ready"] = False
            entry["hint"] = item.problem
        roles[role] = entry
    failing = [e["hint"] for e in roles.values() if "ready" in e and e["ready"] is not True]
    hint = (failing + config.problems)[:1]
    return {
        "roles": roles,
        "roles_ready": None if not config.configured else not failing and not config.problems,
        "roles_hint": hint[0] if hint else None,
    }


def _active_backend() -> Any | None:
    """The configured backend, or None when its name is not one we implement.

    An unknown backend name is already reported by ``backend_known``; it must
    not turn /health into a 400 for a stranger who typed the wrong name.
    """
    try:
        return build_backend()
    except GatewayError:
        return None


async def health_payload(
    *,
    backend: Any | None = None,
    pulse: PulseLog | None = None,
    failover: FailoverState | None = None,
) -> dict[str, Any]:
    catalog = await asyncio.to_thread(describe_catalog)
    name = active_backend_name()
    host = configured_host()
    chosen = backend if backend is not None else _active_backend()
    if chosen is None:
        model = {
            "backend": name,
            "model": model_for(name),
            "ready": False,
            "hint": f"Unknown backend '{name}'. Backends: {', '.join(sorted(catalog))}.",
        }
    else:
        model = await model_readiness(chosen)
    roles = await roles_status(chosen)
    failover = failover or FailoverState()
    annotate_catalog(
        catalog,
        active=name,
        verdict=model if chosen is not None else None,
        pulse=pulse,
        cooling=failover.cooling_names(OUT_OF_QUOTA),
    )
    payload: dict[str, Any] = {
        "ok": True,
        "ui_api": UI_API_VERSION,
        "service": "nexus",
        "version": __version__,
        "backend": name,
        "backend_source": backend_source(),
        "backend_known": name in catalog,
        "ready": bool(model["ready"]) and roles["roles_ready"] is not False,
        "model": model["model"],
        "model_ready": model["ready"],
        "model_hint": model["hint"],
        **roles,
        "bind": f"{host}:{configured_port()}",
        "loopback_enforced": host in {"127.0.0.1", "localhost"},
        "codex_required": False,
        "openrouter": catalog["openrouter"],
        "backends": catalog,
        "routes": describe_routes(),
        "fallback": failover.snapshot(fallback_config(), name),
    }
    if rabbit_watchdog_enabled():
        payload["watchdog"] = WATCHDOG.snapshot()
    if isinstance(model.get("upstream_status"), int):
        payload["upstream_status"] = model["upstream_status"]
    return payload


async def doctor_payload(
    *,
    backend: Any | None = None,
    pulse: PulseLog | None = None,
    failover: FailoverState | None = None,
) -> dict[str, Any]:
    payload = await health_payload(backend=backend, pulse=pulse, failover=failover)
    path = bridge_key_path()
    payload["bridge_key"] = {
        "present": read_bridge_key() is not None,
        "file": str(path),
        "mode": private_mode(path),
    }
    payload["backend_choice_file"] = str(backend_choice_path())
    payload["endpoints"] = list(ENDPOINTS)
    payload["streaming"] = True
    payload["auth"] = "bearer"
    payload["rotate"] = {
        "bridge_key_file": str(path),
        "bridge_key_reload": "per_request",
        "openrouter_key_file": str(openrouter_key_path()),
        "openai_key_file": str(openai_key_path()),
        "anthropic_key_file": str(anthropic_key_path()),
        "xai_key_file": str(xai_key_path()),
        "nous_key_file": str(nous_key_path()),
        "openrouter_key_reload": "per_request_for_file_restart_for_env",
        "provider_key_reload": "per_request_for_file_restart_for_env",
    }
    return payload
