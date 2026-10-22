"""Browser-facing request guard for the unauthenticated routes.

The dashboard and its write endpoint need no bearer token because the door
binds loopback. That argument covers other machines. It does not cover a web
page open in a browser on this machine: that page can make the browser send
requests to 127.0.0.1 (CSRF), and a hostile DNS name can point at 127.0.0.1
and arrive with a foreign ``Host`` (DNS rebinding). Both would reach
``POST /dashboard/config`` and could swap the backend or the provider key.

So every route outside ``/v1`` must arrive with a loopback ``Host``. A write
must also be same-origin JSON: a cross-origin page cannot send
``application/json`` without a CORS preflight, which the door never grants.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from nexus.errors import dashboard_error

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


_SECRET_READS = frozenset({"/dashboard/bridge-key"})


def _foreign(headers: dict[str, str], host: str, what: str) -> tuple[str, str] | None:
    site = headers.get("sec-fetch-site", "")
    if site and site not in {"same-origin", "none"}:
        return "cross_site_refused", f"Cross-site {what} are refused."
    origin = headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != host.strip().lower():
        return "origin_mismatch", "Origin does not match this door."
    return None


def _own_page(headers: dict[str, str], host: str) -> bool:
    """True only for the door's own page: a same-origin fetch, or a loopback Origin on our port.

    curl, scripts and address-bar navigation send neither, so they are refused: a
    non-browser caller reads the key file instead.
    """
    if headers.get("sec-fetch-site", "") == "same-origin":
        return True
    origin = headers.get("origin")
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme != "http" or parts.netloc.lower() != host.strip().lower():
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    from nexus.config import configured_port

    return _host_name(parts.netloc) in LOOPBACK_HOSTS and port == configured_port()


PROVENANCE_CODE = "provenance_required"


def refusal(method: str, headers: dict[str, str], path: str = "") -> tuple[str, str] | None:
    """Return ``(code, reason)`` for a non-/v1 request to refuse, or None to let it through."""
    host = headers.get("host", "")
    if _host_name(host) not in LOOPBACK_HOSTS:
        return "host_refused", "Host must be 127.0.0.1 or localhost."
    if method in _SAFE_METHODS:
        if path not in _SECRET_READS:
            return None
        found = _foreign(headers, host, "bridge key reads")
        if found is not None:
            return found
        if not _own_page(headers, host):
            return (
                PROVENANCE_CODE,
                "The bridge key is only served to the Nexus dashboard in a browser.",
            )
        return None
    found = _foreign(headers, host, "writes")
    if found is not None:
        return found
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return "json_required", "Writes must be sent as application/json."
    return None


def check_request(method: str, headers: dict[str, str]) -> str | None:
    """Return why a non-/v1 request must be refused, or None to let it through."""
    found = refusal(method, headers)
    return None if found is None else found[1]


def provenance_error(reason: str) -> dict[str, object]:
    """OpenAI-style body for a bridge-key read with no browser provenance."""
    from nexus.config import bridge_key_path

    path = str(bridge_key_path())
    return {
        "ok": False,
        "error": {
            "message": f"{reason} Scripts and tools read the key file instead: {path}",
            "type": "permission_error",
            "code": PROVENANCE_CODE,
            "hint": f"Read the key from {path} (owner-only file). Never send it to another host.",
            "key_file": path,
        },
    }


class LoopbackGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path == "/v1" or path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        found = refusal(scope["method"], headers, path)
        if found is None:
            await self.app(scope, receive, send)
            return
        code, reason = found
        if code == PROVENANCE_CODE:
            body = json.dumps(provenance_error(reason)).encode("utf-8")
        else:
            body = json.dumps(dashboard_error(reason, code)).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
