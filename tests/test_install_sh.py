"""install.sh: idempotent, no sudo, ends by running `./nexus test`. Only curl updates."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUB = """#!/usr/bin/env bash
echo "$*" >> "$(dirname "$0")/calls.log"
if [[ "$1" == "test" ]]; then exit "${STUB_TEST_RC:-0}"; fi
if [[ "$1" == "start" ]]; then exit "${STUB_START_RC:-0}"; fi
"""


def _checkout(path: Path, *, configured: bool = True) -> Path:
    (path / "os3_bridge").mkdir(parents=True)
    (path / "os3_bridge" / "__init__.py").write_text("", encoding="utf-8")
    (path / "requirements.txt").write_text("", encoding="utf-8")
    shutil.copy(ROOT / "install.sh", path / "install.sh")
    stub = path / "nexus"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    if configured:
        (path / ".os3_bridge_backend").write_text("openrouter\n", encoding="utf-8")
    return path


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OS3_BRIDGE_", "NEXUS_"))}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _install(
    checkout: Path, home: Path, *, stdin: str | None = None, **extra: str
) -> subprocess.CompletedProcess[str]:
    if stdin is None:
        cmd, feed = ["bash", str(checkout / "install.sh")], None
    else:
        cmd, feed = ["bash"], stdin
    return subprocess.run(
        cmd,
        cwd=checkout if stdin is None else home,
        input=feed,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=_env(home, **extra),
        start_new_session=True,
    )


def _calls(checkout: Path) -> list[str]:
    log = checkout / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_install_is_idempotent_and_ends_with_the_test(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "nexus")
    first = _install(checkout, tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (checkout / ".venv" / "bin" / "python").exists()
    marker = checkout / ".venv" / "keep-me"
    marker.write_text("x", encoding="utf-8")
    second = _install(checkout, tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Reusing .venv" in second.stdout
    assert marker.exists()
    assert _calls(checkout) == ["start", "test --wait 20"] * 2


def test_a_venv_without_pip_is_recreated_not_reused(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "nexus")
    bin_dir = checkout / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    broken = bin_dir / "python"
    broken.write_text('#!/bin/sh\n[ "$1" = "-m" ] && exit 1\nexit 0\n', encoding="utf-8")
    broken.chmod(0o755)
    result = _install(checkout, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Reusing .venv" not in result.stdout
    pip = subprocess.run(
        [str(bin_dir / "python"), "-m", "pip", "--version"], capture_output=True, check=False
    )
    assert pip.returncode == 0


def test_install_exit_code_is_the_tests(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "nexus")
    failed = _install(checkout, tmp_path, STUB_TEST_RC="1")
    assert failed.returncode == 1
    assert _calls(checkout)[-1] == "test --wait 20"


def test_unconfigured_without_a_terminal_stops_with_next_steps(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "nexus", configured=False)
    done = _install(checkout, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "./nexus setup --backend <id> --non-interactive && ./nexus start" in done.stdout
    assert _calls(checkout) == []


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _commit(repo: Path, message: str) -> None:
    subprocess.run([*GIT, "add", "-A", "-f"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", message], cwd=repo, check=True)


def _curl_setup(tmp_path: Path) -> tuple[Path, Path, Path, str, dict[str, str]]:
    if shutil.which("git") is None:
        pytest.skip("needs git")
    source = _checkout(tmp_path / "source")
    subprocess.run([*GIT, "init", "-q"], cwd=source, check=True)
    _commit(source, "init")
    home = tmp_path / "home"
    home.mkdir()
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    return source, home, home / "nexus", script, {"NEXUS_REPO_URL": f"file://{source}"}


def test_curl_form_clones_then_fast_forwards_an_existing_checkout(tmp_path: Path) -> None:
    source, home, target, script, extra = _curl_setup(tmp_path)
    first = _install(source, home, stdin=script, **extra)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (target / "os3_bridge" / "__init__.py").is_file()
    assert (target / ".venv" / "bin" / "python").exists()
    assert _calls(target) == ["start", "test --wait 20"]
    (source / "NEW").write_text("later", encoding="utf-8")
    _commit(source, "later")
    again = _install(source, home, stdin=script, **extra)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Updated" in again.stdout
    assert (target / "NEW").read_text(encoding="utf-8") == "later"
    assert _calls(target) == ["start", "test --wait 20"] * 2


def test_curl_form_warns_and_keeps_a_diverged_checkout(tmp_path: Path) -> None:
    source, home, target, script, extra = _curl_setup(tmp_path)
    assert _install(source, home, stdin=script, **extra).returncode == 0
    (target / "mine.txt").write_text("local", encoding="utf-8")
    _commit(target, "local work")
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True, check=True
    ).stdout
    (source / "NEW").write_text("later", encoding="utf-8")
    _commit(source, "later")
    again = _install(source, home, stdin=script, **extra)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "could not fast-forward" in again.stderr
    assert not (target / "NEW").exists()
    assert (target / "mine.txt").read_text(encoding="utf-8") == "local"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True, check=True
    ).stdout
    assert head == local_head
    assert _calls(target)[-2:] == ["start", "test --wait 20"]


def test_curl_form_warns_and_keeps_a_dirty_checkout(tmp_path: Path) -> None:
    source, home, target, script, extra = _curl_setup(tmp_path)
    (source / "notes.txt").write_text("original\n", encoding="utf-8")
    _commit(source, "notes")
    assert _install(source, home, stdin=script, **extra).returncode == 0
    (target / "notes.txt").write_text("edited locally\n", encoding="utf-8")
    (source / "notes.txt").write_text("edited upstream\n", encoding="utf-8")
    _commit(source, "upstream edit")
    again = _install(source, home, stdin=script, **extra)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "could not fast-forward" in again.stderr
    assert (target / "notes.txt").read_text(encoding="utf-8") == "edited locally\n"
    assert _calls(target)[-2:] == ["start", "test --wait 20"]


def test_running_install_sh_from_inside_the_clone_never_pulls(tmp_path: Path) -> None:
    source, home, target, script, extra = _curl_setup(tmp_path)
    assert _install(source, home, stdin=script, **extra).returncode == 0
    (source / "NEW").write_text("later", encoding="utf-8")
    _commit(source, "later")
    again = _install(target, home, **extra)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Updated" not in again.stdout
    assert not (target / "NEW").exists()


def test_start_failing_with_nothing_listening_points_at_start_or_run(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    checkout = _checkout(tmp_path / "nexus")
    done = _install(checkout, tmp_path, STUB_START_RC="3", OS3_BRIDGE_PORT=str(free_port))
    assert done.returncode == 3, done.stdout + done.stderr
    assert "./nexus run in a terminal" in done.stdout
    assert "Next: ./nexus test" not in done.stdout
    assert _calls(checkout) == ["start"]


def test_start_refused_by_a_foreign_listener_still_runs_the_test(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "nexus")
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = str(holder.getsockname()[1])
        done = _install(
            checkout, tmp_path, STUB_START_RC="1", STUB_TEST_RC="1", OS3_BRIDGE_PORT=port
        )
        assert done.returncode == 1, done.stdout + done.stderr
        assert _calls(checkout) == ["start", "test"]
        ok = _install(checkout, tmp_path, STUB_START_RC="1", STUB_TEST_RC="0", OS3_BRIDGE_PORT=port)
        assert ok.returncode == 0, ok.stdout + ok.stderr


def test_curl_form_refuses_a_directory_that_is_not_a_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "nexus").mkdir(parents=True)
    (home / "nexus" / "mine.txt").write_text("keep", encoding="utf-8")
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    done = _install(ROOT, home, stdin=script)
    assert done.returncode == 1
    assert "not a Nexus checkout" in done.stderr
    assert (home / "nexus" / "mine.txt").read_text(encoding="utf-8") == "keep"


def test_install_script_has_no_sudo_or_destructive_git_or_listener_handling() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for banned in (
        "sudo ",
        "git reset",
        "git stash",
        "git clean",
        "git checkout",
        "--force",
        "--hard",
        "kill ",
        "lsof",
        "launchctl",
        "install -U",
    ):
        assert banned not in code
    assert code.count("pull --ff-only") == 1
