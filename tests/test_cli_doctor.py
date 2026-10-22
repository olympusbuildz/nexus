"""`nexus doctor` runs the checkout's doctor.sh instead of failing as an invalid choice."""

from __future__ import annotations

from pathlib import Path

import pytest
from nexus import cli


def test_doctor_hands_over_to_doctor_sh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "doctor.sh").write_text("exit 7\n", encoding="utf-8")
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    ran: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli.os, "execvp", lambda f, argv: ran.append((f, argv)))
    cli.main(["doctor"])
    assert ran == [("bash", ["bash", str(tmp_path / "doctor.sh")])]


def test_doctor_without_the_script_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    assert cli.main(["doctor"]) == 1
    assert "doctor.sh is missing" in capsys.readouterr().err
