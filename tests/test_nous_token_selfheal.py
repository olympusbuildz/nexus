"""``./nexus start`` must refresh the Nous token or refuse to open the door.

These tests fail on a tree that only documents hourly sync: start would install
the packaged service with a missing or stale key and leave /health ready.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from nexus.cli import main
from nexus.nous_token import (
    REFRESH_INTERVAL_SECONDS,
    SyncResult,
    needs_hermes_sync,
    sync_nous_token,
)
from nexus.service import PackagedService

from tests.test_service import FakePlatform, _patch_start


def _b64(obj: dict) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(exp: int) -> str:
    header = _b64({"alg": "HS256", "typ": "JWT"})
    claims = {"sub": "nous", "scope": "chat", "exp": exp}
    return f"{header}.{_b64(claims)}.signature"


def _write_hermes_auth(home: Path, token: str) -> Path:
    auth_dir = home / ".hermes"
    auth_dir.mkdir(parents=True)
    path = auth_dir / "auth.json"
    path.write_text(
        json.dumps({"providers": {"nous": {"access_token": token}}}),
        encoding="utf-8",
    )
    return path


def _choose_nous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    choice = tmp_path / "backend-choice"
    choice.write_text("nous\n", encoding="utf-8")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(choice))
    key_path = tmp_path / "nous.key"
    monkeypatch.setenv("OS3_BRIDGE_NOUS_KEY_FILE", str(key_path))
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    return key_path


def test_start_refuses_when_nous_token_cannot_refresh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _choose_nous(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-hermes-home"))
    platform = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, platform)

    assert main(["start"]) == 1
    assert platform.events == []
    err = capsys.readouterr().err
    assert "hermes auth add nous" in err
    assert "eyJ" not in err


def test_start_refreshes_nous_token_then_starts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    key_path = _choose_nous(monkeypatch, tmp_path)
    home = tmp_path / "home"
    token = _jwt(int(time.time()) + 3600)
    _write_hermes_auth(home, token)
    monkeypatch.setenv("HOME", str(home))
    platform = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, platform)

    assert main(["start"]) == 0
    assert platform.events == ["start"]
    assert key_path.read_text(encoding="utf-8") == token
    assert oct(key_path.stat().st_mode)[-3:] == "600"
    out = capsys.readouterr().out
    assert "Started packaged nexus" in out
    assert token not in out


def test_non_nous_start_does_not_touch_hermes_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    choice = tmp_path / "backend-choice"
    choice.write_text("openrouter\n", encoding="utf-8")
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(choice))
    calls: list[str] = []

    def boom(**_kwargs: object) -> SyncResult:
        calls.append("sync")
        raise AssertionError("nous sync must not run for openrouter")

    monkeypatch.setattr("nexus.cli.ensure_nous_token", boom)
    monkeypatch.setattr("nexus.cli.needs_hermes_sync", needs_hermes_sync)
    platform = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, platform)

    assert main(["start"]) == 0
    assert platform.events == ["start"]
    assert calls == []


def test_needs_hermes_sync_skips_static_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOUS_API_KEY", "static-not-from-hermes")
    assert needs_hermes_sync("nous") is False
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    assert needs_hermes_sync("nous") is True
    assert needs_hermes_sync("openrouter") is False


def test_sync_nous_token_writes_configured_key_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    token = _jwt(int(time.time()) + 3600)
    auth = _write_hermes_auth(home, token)
    key_path = tmp_path / "bridge.key"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OS3_BRIDGE_NOUS_KEY_FILE", str(key_path))

    result = sync_nous_token(auth_path=auth, key_paths=[key_path])
    assert result.ok
    assert key_path.read_text(encoding="utf-8") == token
    assert token not in result.message


def test_refresh_interval_is_under_token_lifetime() -> None:
    assert 0 < REFRESH_INTERVAL_SECONDS < 60 * 60


def test_start_refuses_expired_hermes_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    key_path = _choose_nous(monkeypatch, tmp_path)
    home = tmp_path / "home"
    _write_hermes_auth(home, _jwt(int(time.time()) - 30))
    monkeypatch.setenv("HOME", str(home))
    platform = FakePlatform([], PackagedService(False, False, None))
    _patch_start(monkeypatch, platform)

    assert main(["start"]) == 1
    assert platform.events == []
    assert not key_path.exists()
    assert "expired" in capsys.readouterr().err
