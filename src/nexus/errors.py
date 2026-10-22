"""Errors rendered in the OpenAI `{error: {message, type, code}}` shape."""

from __future__ import annotations

import re
from typing import Any

from nexus.config import CLI_BIN_ENV, door_origin

_CODE_HINTS = {
    "missing_api_key": (
        "Send 'Authorization: Bearer <bridge key>'. The bridge key is the contents of "
        "the file printed as 'bridge key file:' at startup, not the provider key."
    ),
    "invalid_api_key": (
        "That token is not this bridge's key. Paste the contents of the 'bridge key "
        "file:' printed at startup, or copy it from the dashboard at /."
    ),
    "invalid_json": "Send a JSON object as the body with Content-Type: application/json.",
    "invalid_messages": "messages must be a list of objects with role and content.",
    "invalid_stream": "Set stream to true or false, or omit it.",
    "unknown_endpoint": "Use GET /v1/models, POST /v1/chat/completions or POST /v1/messages.",
}


def fallback_hint(status_code: int, code: str) -> str:
    hint = _CODE_HINTS.get(code)
    if hint:
        return hint
    if code.endswith("_key_missing"):
        backend = code.removesuffix("_key_missing")
        return (
            f"No {backend} key is saved. Run ./nexus setup --backend {backend}, or paste "
            "the key on the dashboard at /, then try again. No restart is needed."
        )
    if status_code == 429:
        return "The upstream is rate limiting or out of quota. Wait and retry, or GET /doctor."
    if status_code >= 500:
        return "The upstream or bridge failed. GET /doctor or ./doctor.sh shows the cause."
    return "Check the request against the OpenAI /v1 shape; GET /doctor shows the backend state."


OUT_OF_QUOTA = "out_of_quota"
RATE_LIMITED = "rate_limited"
UNSUPPORTED = "unsupported"

_QUOTA, _RATE, _MODEL = "quota", "rate", "model"

# One table of "this text means ...". scope is a backend id, "api" (every REST backend),
# "cli" (every subscription CLI) or "*". The wording is each vendor's today, as reported
# by users, and it changes without notice: a row is a best-effort signal, not a contract.
# A miss only means no failover; the original error is returned. Patterns stay on one
# line (no DOTALL) and are anchored to the limit wording, so CLI output that merely
# mentions a word like "quota" is not read as one.
SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("api", _QUOTA, r"insufficient_quota|exceeded your current quota"),  # OpenAI-style body
    ("api", _QUOTA, r"credit balance is too low"),  # Anthropic 400 body
    ("api", _QUOTA, r"requires more credits|insufficient credits|out of credits"),  # OpenRouter
    ("api", _QUOTA, r"quota (?:exceeded|exhausted)"),
    ("api", _MODEL, r"not a valid model|model_not_found|(?:unknown|invalid) model"),
    ("*", _QUOTA, r"usage limit"),
    ("cli", _QUOTA, r"rate limit.*subscription|subscription.*rate limit"),
    ("codex", _QUOTA, r"hit your usage limit|usage_limit_reached"),
    ("chatgpt", _QUOTA, r"usage_limit_reached|usage limit"),
    ("claude-code", _QUOTA, r"(?:5-hour|weekly|session|opus) limit reached|hit your limit"),
    ("copilot", _QUOTA, r"premium requests?.{0,40}(?:exceeded|exhausted|limit|allowance)"),
    ("copilot", _QUOTA, r"(?:exceeded|exhausted).{0,40}(?:premium request|allowance|quota)"),
    ("copilot", _QUOTA, r"quota (?:exceeded|exhausted)"),
    ("grok-build", _QUOTA, r"quota (?:exceeded|exhausted|reached)|out of credits"),
    ("antigravity", _QUOTA, r"quota (?:exceeded|exhausted)|exhausted your (?:capacity|quota)"),
    ("grok-build", _RATE, r"rate limit"),
    ("*", _RATE, r"resource_exhausted|rate_limit_exceeded|too many requests"),
)
_SIGNAL_RES = tuple((scope, kind, re.compile(pat, re.I)) for scope, kind, pat in SIGNALS)
_UNSUPPORTED_CODES = frozenset(
    {"tools_not_supported", "images_not_supported", "image_url_unsupported"}
)
_BODY_STATUSES = frozenset({400, 402, 403, 404, 429})


def _text_kind(text: str, backend_id: str, kinds: tuple[str, ...]) -> str | None:
    """The class ``text`` signals for ``backend_id``. A quota signal outranks a rate one."""
    scopes = {"*", backend_id, "cli" if backend_id in CLI_BIN_ENV else "api"}
    found = {
        kind
        for scope, kind, rx in _SIGNAL_RES
        if kind in kinds and scope in scopes and rx.search(text)
    }
    if _QUOTA in found:
        return OUT_OF_QUOTA
    if _RATE in found:
        return RATE_LIMITED
    return UNSUPPORTED if _MODEL in found else None


def quota_text(text: str, backend_id: str) -> bool:
    """True when ``text`` (CLI stderr/stdout or an upstream body) says the quota is exhausted."""
    return _text_kind(text, backend_id, (_QUOTA,)) == OUT_OF_QUOTA


def classify_failure(exc: GatewayError) -> str | None:
    """``OUT_OF_QUOTA``, ``RATE_LIMITED``, ``UNSUPPORTED`` or None. Pure; no prompt or key."""
    code = str(exc.code)
    if code in _UNSUPPORTED_CODES or code.endswith(("_usage_limit", "_failed", "_empty")):
        return _classify_cli(exc, code)
    upstream = exc.extra.get("upstream")
    if not isinstance(upstream, dict):
        return None
    status = upstream.get("upstream_status")
    backend = str(upstream.get("backend") or "")
    body = upstream.get("upstream_body")
    text = body if isinstance(body, str) else ""
    if status == 429:
        return _text_kind(text, backend, (_QUOTA,)) or RATE_LIMITED
    if status == 404:
        return UNSUPPORTED
    if status in _BODY_STATUSES and text:
        kinds = (_QUOTA, _MODEL) if status == 400 else (_QUOTA,)
        return _text_kind(text, backend, kinds)
    return None


def _classify_cli(exc: GatewayError, code: str) -> str | None:
    if code in _UNSUPPORTED_CODES:
        return UNSUPPORTED
    for suffix in ("_usage_limit", "_failed", "_empty"):
        if code.endswith(suffix):
            backend = code[: -len(suffix)]
            if backend not in CLI_BIN_ENV:
                return None
            kind = _text_kind(exc.message, backend, (_QUOTA, _RATE))
            return kind or (OUT_OF_QUOTA if suffix == "_usage_limit" else None)
    return None


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        err_type: str,
        code: str,
        *,
        extra: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code
        self.message = message
        self.err_type = err_type
        self.code = code
        self.extra = extra or {}

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.message,
            "type": self.err_type,
            "code": self.code,
            "hint": fallback_hint(self.status_code, self.code),
        }
        if self.extra:
            error.update(self.extra)
        return {"error": error}


_DASHBOARD_HINTS = {
    "cross_site_refused": (
        "Send this request from the page the bridge serves at /, not from another site."
    ),
    "origin_mismatch": (
        "Send this request from the page the bridge serves at /, not from another origin."
    ),
    "json_required": "Send the body as JSON with Content-Type: application/json.",
    "invalid_json": "Send a JSON object as the body with Content-Type: application/json.",
    "invalid_request": "Check the request fields against docs/ui-api.md.",
    "unknown_backend": (
        "Use one of: openrouter, openai, anthropic, xai, nous, codex, claude-code, "
        "copilot, grok-build, antigravity. GET /health lists them."
    ),
    "key_missing": (
        "Paste a provider key in the Key field and save, or put one in a mode-600 key file."
    ),
    "key_invalid": "Paste the provider key as one line with no spaces. A placeholder is not a key.",
    "key_rejected": (
        "The provider refused that key. Create a new one in the provider console and paste it."
    ),
    "key_unverified": (
        "The provider could not be reached to check the key. Check the connection and retry."
    ),
    "cli_missing": (
        "Install the official CLI, sign in there, then try again. GET /health shows its state."
    ),
    "not_ready": "Nothing was changed. Fix this, then Save again.",
    "cli_has_no_key": "A subscription CLI signs in with its own login. Leave the key field empty.",
    "write_failed": (
        "The bridge could not write its file. Check the directory is writable by this user."
    ),
    "bench_busy": "A bench is already running. Wait for it to finish, then start another.",
    "bench_limits": (
        "Use 1 to 6 targets, a prompt up to 2000 characters, max_tokens 1 to 256, "
        "and timeout_s 1 to 60."
    ),
    "internal_error": "The bridge failed. GET /doctor or ./doctor.sh shows the cause.",
}


def dashboard_hint(code: str) -> str:
    if code == "host_refused":
        return f"Open the page at {door_origin()}/, not through another hostname."
    return _DASHBOARD_HINTS.get(code, "GET /doctor shows the backend state.")


def dashboard_error(error: str, code: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    """The control-plane error body. ``error`` stays a string, so a reader that only
    knows the older ``{ok, error}`` shape keeps working."""
    return {
        "ok": False,
        "error": error,
        "code": code,
        "hint": hint or dashboard_hint(code),
        **extra,
    }


class DashboardError(Exception):
    """A control-plane failure rendered by ``dashboard_error`` at ``status_code``."""

    def __init__(self, status_code: int, error: str, code: str, hint: str | None = None) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.code = code
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        return dashboard_error(self.error, self.code, self.hint)
