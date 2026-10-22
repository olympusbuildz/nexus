"""NEXUS_PORT is honoured by run, start and status; NEXUS_SERVICE_LABEL by the LaunchAgent."""

from __future__ import annotations

from pathlib import Path

import pytest
from nexus import cli, service
from nexus.config import repo_root
from nexus.service import (
    LAUNCHD_LABEL,
    CommandResult,
    LaunchdPlatform,
    Listener,
    ServiceError,
    busy_message,
    door_environment,
    launchd_label,
    render_installed_unit,
)


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("NEXUS_PORT", "11437")
    return 11437


def _spy(monkeypatch: pytest.MonkeyPatch, found: list[Listener] | None = None) -> list[int]:
    seen: list[int] = []

    def collect(port: int, **_kw: object) -> list[Listener]:
        seen.append(port)
        return list(found or [])

    monkeypatch.setattr(service, "collect_listeners", collect)
    return seen


def test_run_checks_the_configured_port(port: int, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    seen = _spy(monkeypatch, [Listener(pid=4242, command="python3 server.py")])
    monkeypatch.setattr(cli, "current_service_pid", lambda: None)
    assert cli._cmd_run() == 1
    assert seen == [port]
    assert f"127.0.0.1:{port} is already in use" in capsys.readouterr().err


def test_run_default_port_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_PORT", raising=False)
    monkeypatch.delenv("OS3_BRIDGE_PORT", raising=False)
    seen = _spy(monkeypatch)
    service.live_listeners()
    assert seen == [11435]


def test_status_reports_the_configured_port(
    port: int, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    seen = _spy(monkeypatch)

    class Fake:
        label = "x"

        def packaged(self) -> service.PackagedService:
            return service.PackagedService(False, False, None)

        def listeners(self) -> list[Listener]:
            return service.live_listeners()

    monkeypatch.setattr(cli, "build_platform", lambda: Fake())
    assert cli._cmd_status() == 0
    out = capsys.readouterr().out
    assert f"port {port}: free" in out and "11435" not in out
    assert seen == [port]


def test_start_messages_and_unit_use_the_configured_port(port: int) -> None:
    assert busy_message([Listener(pid=1, command="x")]).startswith(f"127.0.0.1:{port} ")
    env = door_environment("openrouter")
    assert env["NEXUS_PORT"] == str(port)
    plist = render_installed_unit(
        "launchd", repo=repo_root(), python=Path("/usr/bin/python3"), env=env
    )
    assert f"<string>{port}</string>" in plist


def test_unit_refuses_a_port_other_than_the_configured_one(port: int) -> None:
    env = {**door_environment("openrouter"), "NEXUS_PORT": "11435"}
    with pytest.raises(ServiceError, match=str(port)):
        render_installed_unit("launchd", repo=repo_root(), python=Path("/usr/bin/python3"), env=env)


def test_label_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NEXUS_SERVICE_LABEL", raising=False)
    assert launchd_label() == LAUNCHD_LABEL == "ai.nexus.server"
    monkeypatch.setenv("NEXUS_SERVICE_LABEL", "com.example.nexus-test")
    calls: list[list[str]] = []
    platform = LaunchdPlatform(
        tmp_path, run=lambda argv: calls.append(argv) or CommandResult(1, "", "")
    )
    assert platform.label == "com.example.nexus-test"
    assert platform.plist.name == "com.example.nexus-test.plist"
    platform.packaged()
    platform.stop_and_remove()
    assert all(LAUNCHD_LABEL not in " ".join(c) for c in calls)
    assert any(c[-1].endswith("/com.example.nexus-test") for c in calls)
    env = door_environment("openrouter")
    assert env["NEXUS_SERVICE_LABEL"] == "com.example.nexus-test"
    plist = render_installed_unit(
        "launchd", repo=repo_root(), python=Path("/usr/bin/python3"), env=env
    )
    assert "<string>com.example.nexus-test</string>" in plist
    assert LAUNCHD_LABEL not in plist.split("<key>Label</key>", 1)[1].split("</string>", 1)[0]


@pytest.mark.parametrize("bad", ["../evil", "a b", "x;rm", "-lead"])
def test_label_rejects_unsafe_values(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("NEXUS_SERVICE_LABEL", bad)
    with pytest.raises(ServiceError):
        launchd_label()


def test_door_addresses_in_the_page_and_hints_follow_nexus_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient
    from nexus.app import create_app
    from nexus.errors import dashboard_hint

    monkeypatch.setenv("NEXUS_PORT", "11500")
    with TestClient(create_app(), base_url="http://127.0.0.1:11500") as client:
        page = client.get("/").text
        bind = client.get("/health").json()["bind"]
    assert bind == "127.0.0.1:11500"
    assert "11435" not in page
    assert "127.0.0.1:11500/" in dashboard_hint("host_refused")
