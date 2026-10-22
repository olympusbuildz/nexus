"""Refuse to start on a default model the configured backend cannot serve.

The failure this exists to stop: a stranger installs the package, pastes an
OpenRouter key, and the bridge comes up green. A listening door, a valid key
and an ``ok`` /health is the worst combination, because nothing is actually
broken until the first real request -- and by then the error is a bare 404
from upstream naming a model they never chose.

So the door checks itself before it opens. If the model the bridge is about to
send is *known* to be absent from a configured backend, startup fails loudly
with the model named and a pointer to the override.

Deliberately not fatal:

* no key, or a backend that is not ready -- there is nothing to check against,
  and refusing to start would break the setup flow that puts the key in place;
* a /models call that could not be completed, or answered ambiguously. That is
  "unknowable", not "broken": a flaky network or a provider outage must not
  keep the bridge down. Those cases return ``None`` and startup continues.

Only a confirmed negative stops the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus.backends.registry import active_backend_name, build_backend
from nexus.config import default_model, model_for


class DefaultModelUnavailable(RuntimeError):
    """The configured default model is not available on a configured backend."""


@dataclass(frozen=True)
class BackendCheck:
    """What one backend could tell us about one model id."""

    backend: str
    model: str
    reachable: bool | None
    detail: str = ""

    @property
    def confirmed_missing(self) -> bool:
        """True only on a positive answer that the model is absent."""
        return self.reachable is False


def configured_backends() -> list[str]:
    """Backend ids to validate against, active one first.

    The active backend is what the bridge will actually send to, so it is
    checked first. The rest follow so that a bad default is caught even when
    the operator has switched away from the backend that shipped it.
    """
    try:
        active = active_backend_name()
    except Exception:
        active = ""
    ordered: list[str] = []
    for name in (active, "openrouter", "openai", "anthropic", "xai"):
        if name and name not in ordered:
            ordered.append(name)
    return ordered


def _describe(backend: Any) -> dict[str, Any]:
    try:
        described = backend.describe()
    except Exception:
        return {}
    return described if isinstance(described, dict) else {}


async def _check_one(backend_id: str, model: str) -> BackendCheck:
    """Ask one backend whether it serves ``model``. Never raises."""
    try:
        backend = build_backend(backend_id)
    except Exception:
        return BackendCheck(backend_id, model, None, "backend could not be built")
    if _describe(backend).get("ready") is not True:
        return BackendCheck(backend_id, model, None, "backend is not configured")
    try:
        reachable = await backend.model_reachable(model)
    except Exception:
        return BackendCheck(backend_id, model, None, "the model list could not be read")
    return BackendCheck(backend_id, model, reachable)


async def check_default_model(backends: list[str] | None = None) -> list[BackendCheck]:
    """Check the effective default against every configured backend.

    A backend reporting "no" for a model it does not serve is not evidence
    about the others -- ``gpt-4o-mini`` is missing from OpenRouter and from
    Anthropic alike, while being perfectly valid for OpenAI. So a default is
    only rejected when *no* configured backend can serve it.
    """
    names = configured_backends() if backends is None else list(backends)
    return [await _check_one(name, model_for(name)) for name in names]


def _format(checks: list[BackendCheck]) -> str:
    lines = [
        "nexus: the configured default model is not available on any configured backend.",
        "",
        f"  model: {default_model()}",
        "",
        "Checked:",
    ]
    for check in checks:
        if check.reachable is True:
            state = "ok"
        elif check.reachable is False:
            state = "NOT AVAILABLE"
        else:
            state = f"unverified ({check.detail or 'unknown'})"
        lines.append(f"  {check.backend:<12} {state}")
    lines += [
        "",
        "Every request would fail until this is fixed. Either set",
        "NEXUS_MODEL to a model your account can reach, or run",
        "./nexus setup to pick a working default.",
        "To see what your account offers, list the upstream models:",
        '  curl -H "Authorization: Bearer $OPENROUTER_API_KEY" \\',
        "    https://openrouter.ai/api/v1/models",
    ]
    return "\n".join(lines)


async def enforce_default_model(backends: list[str] | None = None) -> list[BackendCheck]:
    """Raise ``DefaultModelUnavailable`` when the default cannot be served.

    Returns the checks it made when startup may continue, so a caller can log
    or print the outcome.
    """
    checks = await check_default_model(backends)
    if not checks:
        return checks
    if all(check.reachable is not True for check in checks) and any(
        check.confirmed_missing for check in checks
    ):
        raise DefaultModelUnavailable(_format(checks))
    return checks
