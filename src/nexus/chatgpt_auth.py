"""ChatGPT plan credentials, borrowed from the Codex CLI login. Never logged or returned.

``codex login`` (Sign in with ChatGPT) writes ``$CODEX_HOME/auth.json`` (default
``~/.codex/auth.json``). This module reads that file at request time and, when the
access token is about to expire, refreshes it the way the Codex CLI does: one POST
to ``auth.openai.com/oauth/token`` with the refresh token, then an atomic, mode-600
write back to the same file so the CLI and the bridge keep sharing one login.

Refresh tokens rotate, so two refreshers racing would invalidate each other. The
refresh therefore holds an exclusive ``flock`` on a sidecar lock file and re-reads
the file under it: if another process (or the CLI) already refreshed, that token is
used and no second refresh is sent.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nexus.config import environ_get
from nexus.errors import GatewayError

REFRESH_URL = "https://auth.openai.com/oauth/token"
# The Codex CLI's public OAuth client id (codex-rs/login, CLIENT_ID).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_WINDOW_S = 300.0
LOGIN_HINT = "Run `codex login` in a terminal and choose Sign in with ChatGPT."


@dataclass(frozen=True)
class Credentials:
    access_token: str
    account_id: str
    expires_at: float | None

    def __repr__(self) -> str:
        return f"Credentials(account_id=<set>, expires_at={self.expires_at})"


def auth_path() -> Path:
    raw = (environ_get("NEXUS_CHATGPT_AUTH_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    home = (os.environ.get("CODEX_HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "auth.json"


def jwt_exp(token: str) -> float | None:
    """The ``exp`` claim of a JWT, without verifying it. None when unreadable."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode()))
    except (IndexError, ValueError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, int | float) else None


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tokens(data: dict[str, Any] | None) -> dict[str, Any] | None:
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    if not all(isinstance(tokens.get(k), str) and tokens.get(k) for k in ("access_token",)):
        return None
    return tokens


def _account_id(tokens: dict[str, Any]) -> str | None:
    account = tokens.get("account_id")
    return account if isinstance(account, str) and account else None


def login_status() -> tuple[str, str]:
    """(``ok`` | ``missing`` | ``unusable``, hint). Reads the file; no network, no secret out."""
    path = auth_path()
    if not path.is_file():
        return "missing", f"No ChatGPT login at {path}. {LOGIN_HINT}"
    data = _read(path)
    tokens = _tokens(data)
    if tokens is None or _account_id(tokens) is None:
        if isinstance(data, dict) and data.get("OPENAI_API_KEY"):
            return "unusable", (
                f"{path} holds an API-key login, not a ChatGPT plan login. {LOGIN_HINT}"
            )
        return "unusable", f"{path} has no ChatGPT tokens. {LOGIN_HINT}"
    exp = jwt_exp(str(tokens["access_token"]))
    if exp is not None and exp <= time.time() and not tokens.get("refresh_token"):
        return "unusable", f"The ChatGPT login in {path} expired. {LOGIN_HINT}"
    return "ok", "ok"


def _fresh(tokens: dict[str, Any], now: float) -> bool:
    exp = jwt_exp(str(tokens["access_token"]))
    return exp is None or exp - now > REFRESH_WINDOW_S


def _creds(tokens: dict[str, Any]) -> Credentials:
    account = _account_id(tokens)
    if account is None:
        raise _not_logged_in("has no ChatGPT account id")
    token = str(tokens["access_token"])
    return Credentials(access_token=token, account_id=account, expires_at=jwt_exp(token))


def _not_logged_in(why: str) -> GatewayError:
    return GatewayError(
        401,
        f"The ChatGPT login {why}.",
        "authentication_error",
        "chatgpt_not_logged_in",
        extra={"hint": LOGIN_HINT},
    )


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


_process_lock = asyncio.Lock()


async def access(*, force_refresh: bool = False) -> Credentials:
    """A usable access token and account id. Refreshes when it is close to expiry.

    ``force_refresh`` is for one retry after the backend answered 401.
    """
    path = auth_path()
    tokens = _tokens(_read(path))
    if tokens is None:
        raise _not_logged_in(f"is missing ({path})")
    if not force_refresh and _fresh(tokens, time.time()):
        return _creds(tokens)
    async with _process_lock:
        return await asyncio.to_thread(_refresh_locked, path, tokens, force_refresh)


def _refresh_locked(
    path: Path,
    seen: dict[str, Any],
    force: bool,
) -> Credentials:
    lock_path = path.with_name(path.name + ".nexus.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = _read(path)
            tokens = _tokens(data)
            if tokens is None or data is None:
                raise _not_logged_in(f"is missing ({path})")
            changed = tokens.get("access_token") != seen.get("access_token")
            if changed or (not force and _fresh(tokens, time.time())):
                return _creds(tokens)
            refresh = tokens.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise _not_logged_in("expired and has no refresh token")
            fresh = _post_refresh(refresh)
            for key in ("id_token", "access_token", "refresh_token"):
                value = fresh.get(key)
                if isinstance(value, str) and value:
                    tokens[key] = value
            data["tokens"] = tokens
            data["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            _write_atomic(path, data)
            return _creds(tokens)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _post_refresh(refresh: str) -> dict[str, Any]:
    body = {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh}
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(REFRESH_URL, json=body)
    except httpx.HTTPError:
        raise GatewayError(
            502,
            "Could not reach auth.openai.com to refresh the ChatGPT login",
            "upstream_error",
            "chatgpt_refresh_unreachable",
        ) from None
    if response.status_code in {400, 401}:
        try:
            code = str((response.json() or {}).get("error", ""))
        except ValueError:
            code = ""
        why = {
            "refresh_token_expired": "expired",
            "refresh_token_reused": "was refreshed elsewhere and is no longer valid",
            "refresh_token_invalidated": "was revoked",
        }.get(code, "could not be refreshed")
        raise _not_logged_in(why)
    if response.status_code >= 400:
        raise GatewayError(
            502,
            f"ChatGPT login refresh failed (HTTP {response.status_code})",
            "upstream_error",
            "chatgpt_refresh_failed",
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise GatewayError(
            502,
            "ChatGPT login refresh returned no token",
            "upstream_error",
            "chatgpt_refresh_failed",
        )
    return payload
