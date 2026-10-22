"""Bridge bearer token. The file is re-read on every /v1 request."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading

from fastapi import Header

from nexus.config import bridge_key_path
from nexus.errors import GatewayError


def read_bridge_key() -> str | None:
    path = bridge_key_path()
    try:
        if not path.is_file():
            return None
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def ensure_bridge_key() -> str:
    """Return the bridge key, creating it once even when callers race.

    The key is written to a private temp file and then linked into place. link()
    fails when the target exists, so exactly one writer wins and a reader never
    sees a half-written or truncated file. Every caller returns what is on disk.
    """
    existing = read_bridge_key()
    if existing:
        return existing
    path = bridge_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "os3_" + secrets.token_urlsafe(32)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (key + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        except OSError:
            if read_bridge_key() is None:
                os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return read_bridge_key() or key


def keys_match(provided: str, expected: str) -> bool:
    return secrets.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not str(authorization).strip():
        raise GatewayError(
            401,
            "Missing Bearer token",
            "authentication_error",
            "missing_api_key",
        )
    parts = str(authorization).split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise GatewayError(
            401,
            "Missing Bearer token",
            "authentication_error",
            "missing_api_key",
        )
    token = parts[1].strip()
    expected = ensure_bridge_key()
    if not keys_match(token, expected):
        raise GatewayError(
            401,
            "Invalid API key",
            "authentication_error",
            "invalid_api_key",
        )
