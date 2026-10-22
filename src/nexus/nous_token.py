"""Keep the Nous Portal OAuth token live for the ``nous`` backend.

The token in ``~/.hermes/auth.json`` expires every 60 minutes. Copying it into
the bridge key file used to be a manual hourly chore; missing that chore left
``/health`` ready while every request returned 401. This module is the single
place that reads Hermes auth, validates ``exp``, and writes the key file.

Why an in-process refresh task (not a second LaunchAgent/cron job):
``./nexus start`` already installs one packaged unit that runs the door.
A second scheduler is another unit to install, drift, and fail silently — the
exact failure mode that made the hourly script worthless. The bridge re-reads
the key file on every request (see ``config.py``), so rewriting the file from
inside the same process is enough. Refresh every 45 minutes so a long-lived
bridge does not ride the token past its 60-minute ``exp``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from nexus.config import environ_get, key_write_path, nous_key_path, repo_root
from nexus.redact import redact

logger = logging.getLogger("nexus")

# Token lifetime is 60 minutes. Refresh earlier so a single missed cycle does
# not leave the door serving a dead credential for the rest of the hour.
REFRESH_INTERVAL_SECONDS = 45 * 60


class NousTokenError(Exception):
    """User-facing sync failure. The message must never contain the token."""


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one Hermes → bridge key copy. Never carries token material."""

    ok: bool
    message: str
    paths: tuple[Path, ...] = ()


def hermes_auth_path() -> Path:
    return Path.home() / ".hermes" / "auth.json"


def needs_hermes_sync(backend: str) -> bool:
    """True when this backend depends on the hourly Hermes OAuth token.

    A static ``NOUS_API_KEY`` in the environment is the documented non-Hermes
    path; env wins over the key file, so syncing would not help and must not
    block start.
    """
    if backend != "nous":
        return False
    return not os.environ.get("NOUS_API_KEY", "").strip()


def token_expiry(token: str) -> int:
    """Return the JWT ``exp`` claim of ``token``.

    Raises ValueError when the token is not a decodable JWT or carries no
    integer ``exp`` claim — callers treat that as unusable rather than valid.
    """
    parts = token.split(".")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"payload is not base64url JSON: {exc}") from exc
    if not isinstance(claims, dict) or "exp" not in claims:
        raise ValueError("no exp claim")
    exp = claims["exp"]
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        raise ValueError("exp claim is not a number")
    return int(exp)


def _access_token_from_auth(data: object) -> str | None:
    """Pull the active Nous access_token out of a parsed Hermes auth.json."""
    if not isinstance(data, dict):
        return None
    providers = data.get("providers", {})
    if isinstance(providers, dict):
        nous_provider = providers.get("nous", {})
        if isinstance(nous_provider, dict):
            token = nous_provider.get("access_token")
            if isinstance(token, str) and token.strip():
                return token.strip()
    pool = data.get("credential_pool", {})
    if isinstance(pool, dict):
        nous_creds = pool.get("nous", [])
        if isinstance(nous_creds, list) and nous_creds:
            first = nous_creds[0]
            if isinstance(first, dict):
                token = first.get("access_token")
                if isinstance(token, str) and token.strip():
                    return token.strip()
    return None


def destination_key_paths() -> list[Path]:
    """Where the live token must land so the bridge can read it.

    Always write the configured key path. When the operator did not override
    ``NEXUS_NOUS_KEY_FILE``, also write the checkout-local file that the
    original sync script stamped for developer machines. An env override is
    the single source of truth (tests and custom deployments).
    """
    primary = nous_key_path()
    if (environ_get("NEXUS_NOUS_KEY_FILE") or "").strip():
        return [primary]
    primary = key_write_path(primary, "NEXUS_NOUS_KEY_FILE")
    local = repo_root() / ".nous-os3.key"
    if local.resolve() == primary.resolve():
        return [primary]
    return [primary, local]


def sync_nous_token(
    *,
    auth_path: Path | None = None,
    key_paths: list[Path] | None = None,
) -> SyncResult:
    """Copy a live Nous access token from Hermes auth into the bridge key file(s).

    Never returns or logs the token itself. Callers that need a hard failure
    use :func:`ensure_nous_token`.
    """
    source = auth_path if auth_path is not None else hermes_auth_path()
    if not source.is_file():
        return SyncResult(
            False,
            f"ERROR: {source} not found. Run `hermes auth add nous` first.",
        )

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SyncResult(False, f"ERROR: cannot read {source}: {exc}")

    token = _access_token_from_auth(data)
    if token is None:
        return SyncResult(
            False,
            "ERROR: no Nous access_token found in the auth store. "
            "Run `hermes auth add nous` first.",
        )

    try:
        exp = token_expiry(token)
    except ValueError as exc:
        return SyncResult(
            False,
            "ERROR: source token in ~/.hermes/auth.json is not a readable JWT "
            f"({exc}); run 'hermes auth add nous'",
        )

    now = int(time.time())
    if now > exp:
        return SyncResult(
            False,
            "ERROR: source token in ~/.hermes/auth.json expired at "
            f"{exp}; run 'hermes auth add nous'",
        )

    destinations = list(key_paths) if key_paths is not None else destination_key_paths()
    written: list[Path] = []
    for path in destinations:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            return SyncResult(False, f"ERROR: cannot write {path}: {exc}")
        written.append(path)

    joined = " and ".join(str(path) for path in written)
    return SyncResult(True, f"OK: Nous token synced to {joined}", tuple(written))


def ensure_nous_token(
    *,
    auth_path: Path | None = None,
    key_paths: list[Path] | None = None,
) -> SyncResult:
    """Sync or raise :class:`NousTokenError`.

    Used by ``./nexus start`` and process boot so a dead token cannot
    leave the door green.
    """
    result = sync_nous_token(auth_path=auth_path, key_paths=key_paths)
    if not result.ok:
        raise NousTokenError(result.message)
    return result


async def nous_token_refresh_loop(
    *,
    interval_seconds: float = REFRESH_INTERVAL_SECONDS,
) -> None:
    """Periodically re-copy the Hermes token while the bridge is running.

    Failures are logged (never with token material) and retried next cycle —
    taking the process down mid-request would be worse than a brief 401 window
    after Hermes itself has gone stale. Start already refused to open on a
    failed first sync.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await asyncio.to_thread(sync_nous_token)
        except Exception:
            logger.exception("nous token refresh crashed")
            continue
        text = redact(result.message)
        if result.ok:
            logger.info("%s", text)
        else:
            logger.error("%s", text)
