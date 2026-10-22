"""Serve a built Nexus UI at ``/`` from the same origin as the API.

When ``ui/dist/index.html`` (or ``$OS3_UI_DIST/index.html``) exists, ``GET`` and
``HEAD`` for a path that is not an API route return the matching file from that
directory, or ``index.html`` for a path with no file extension (single-page-app
fallback). When it does not exist this middleware does nothing and the request
reaches the routes exactly as before, so the built-in dashboard at ``/`` is
unchanged.

It sits inside ``LoopbackGuard``: a foreign ``Host`` is refused before a file is
read. It never serves anything outside the dist directory, and ``/v1``,
``/dashboard``, ``/health`` and ``/doctor`` are never its business.
"""

from __future__ import annotations

from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nexus.config import ui_dist_dir

_API_PREFIXES = ("/v1", "/dashboard")
_API_PATHS = frozenset({"/health", "/doctor"})
_METHODS = frozenset({"GET", "HEAD"})
_HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}


def is_api_path(path: str) -> bool:
    if path in _API_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _API_PREFIXES)


def dist_root() -> Path | None:
    """The dist directory, only when it holds an ``index.html``."""
    root = ui_dist_dir()
    try:
        if (root / "index.html").is_file():
            return root.resolve()
    except OSError:
        return None
    return None


def _file_for(root: Path, path: str) -> Path | None:
    """The file under ``root`` for a URL path, or None. Refuses anything that escapes it."""
    relative = path.lstrip("/")
    if not relative or "\x00" in relative:
        return None
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


class UiStaticMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _METHODS:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if is_api_path(path):
            await self.app(scope, receive, send)
            return
        root = dist_root()
        if root is None:
            await self.app(scope, receive, send)
            return
        found = _file_for(root, path)
        if found is None:
            if "." in path.rsplit("/", 1)[-1]:
                await self.app(scope, receive, send)
                return
            found = root / "index.html"
        await FileResponse(found, headers=_HEADERS)(scope, receive, send)
