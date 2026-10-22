"""The hourly Nous token sync must refuse to publish an expired token."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sync-nous-token.py"


def _b64(obj: dict) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(exp: int | None) -> str:
    header = _b64({"alg": "HS256", "typ": "JWT"})
    claims: dict = {"sub": "nous", "scope": "chat"}
    if exp is not None:
        claims["exp"] = exp
    return f"{header}.{_b64(claims)}.signature"


def _run(tmp_path: Path, token: str) -> subprocess.CompletedProcess:
    """Run the real script with HOME and key path isolated from the host."""
    auth_dir = tmp_path / "home" / ".hermes"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"access_token": token}}}),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["OS3_BRIDGE_NOUS_KEY_FILE"] = str(tmp_path / "bridge.key")
    env.pop("NOUS_API_KEY", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def test_expired_token_fails_loudly(tmp_path: Path) -> None:
    exp = int(time.time()) - 60
    result = _run(tmp_path, _jwt(exp))
    assert result.returncode == 1
    assert (
        "ERROR: source token in ~/.hermes/auth.json expired at "
        f"{exp}; run 'hermes auth add nous'" in result.stderr
    )
    assert not (tmp_path / "bridge.key").exists()


def test_valid_token_still_syncs(tmp_path: Path) -> None:
    token = _jwt(int(time.time()) + 3600)
    result = _run(tmp_path, token)
    assert result.returncode == 0, result.stderr
    assert "OK: Nous token synced to" in result.stdout
    key_path = tmp_path / "bridge.key"
    assert key_path.read_text(encoding="utf-8") == token
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_unreadable_token_fails_loudly(tmp_path: Path) -> None:
    result = _run(tmp_path, "not-a-jwt")
    assert result.returncode == 1
    assert "ERROR: source token in ~/.hermes/auth.json is not a readable JWT" in result.stderr
    assert not (tmp_path / "bridge.key").exists()


def test_token_without_exp_claim_fails_loudly(tmp_path: Path) -> None:
    result = _run(tmp_path, _jwt(None))
    assert result.returncode == 1
    assert "no exp claim" in result.stderr
    assert not (tmp_path / "bridge.key").exists()
