"""A CLI the bridge started must not outlive the call that started it.

These tests use a real slow process tree: a shell script that records its pid, starts a
grandchild that records its own, and sleeps. They fail if the process group survives a
timeout or a cancellation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.backends import cli_exec
from nexus.backends.subscription import CodexCliBackend, clear_probe_cache

SLOW_CLI = """#!/bin/sh
case "$1" in
  exec) ;;
  *) echo "Logged in using ChatGPT"; exit 0;;
esac
if [ -n "$OS3_SLOW_IGNORE_TERM" ]; then trap '' TERM; fi
echo $$ > "$OS3_SLOW_DIR/child.pid"
sleep 60 &
echo $! > "$OS3_SLOW_DIR/grandchild.pid"
sleep 60
"""
CHAT = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]}


async def _settle() -> None:
    """Let the loop deliver the pipes' EOF before asyncio.run closes it."""
    await asyncio.sleep(0.2)


def _alive(pid: int) -> bool:
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _pids(directory: Path) -> tuple[int, int]:
    return (
        int((directory / "child.pid").read_text().strip()),
        int((directory / "grandchild.pid").read_text().strip()),
    )


def _wait_for_pids(directory: Path, timeout: float = 5.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _pids(directory)
        except (FileNotFoundError, ValueError):
            time.sleep(0.02)
    raise AssertionError("the fake CLI never recorded its pids")


async def _wait_for_pids_async(directory: Path, timeout: float = 5.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _pids(directory)
        except (FileNotFoundError, ValueError):
            await asyncio.sleep(0.02)
    raise AssertionError("the fake CLI never recorded its pids")


def _assert_gone(pids: tuple[int, int], within: float = 3.0) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            return
        time.sleep(0.05)
    survivors = [pid for pid in pids if _alive(pid)]
    for pid in survivors:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    raise AssertionError(f"CLI processes outlived the call: {survivors}")


@pytest.fixture
def slow_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "slow-codex"
    binary.write_text(SLOW_CLI, encoding="utf-8")
    binary.chmod(0o755)
    pids = tmp_path / "pids"
    pids.mkdir()
    monkeypatch.setenv("OS3_SLOW_DIR", str(pids))
    monkeypatch.setenv("OS3_BRIDGE_CODEX_BIN", str(binary))
    clear_probe_cache()
    return pids


def _bench(client: TestClient, timeout_s: float = 1) -> dict:
    return client.post(
        "/dashboard/bench",
        json={
            "targets": [{"backend": "codex", "model": "gpt-5.6-sol"}],
            "prompt": "hi",
            "timeout_s": timeout_s,
        },
    ).json()


@pytest.mark.parametrize("ignore_term", [False, True], ids=["sigterm", "sigkill-after-grace"])
def test_a_bench_timeout_kills_the_cli_and_its_grandchild(
    slow_cli: Path, monkeypatch: pytest.MonkeyPatch, ignore_term: bool
) -> None:
    if ignore_term:
        monkeypatch.setenv("OS3_SLOW_IGNORE_TERM", "1")
    with TestClient(create_app()) as client:
        started = time.monotonic()
        body = _bench(client)
        elapsed = time.monotonic() - started
    row = body["rows"][0]
    assert row["ok"] is False
    assert row["error"]["code"] == "bench_timeout"
    assert elapsed < 5
    _assert_gone(_pids(slow_cli))


def test_the_bench_lock_is_not_released_before_the_cli_is_gone(
    slow_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_SLOW_IGNORE_TERM", "1")
    app = create_app()
    with TestClient(app) as client:
        _bench(client)
        assert app.state.bench_running is False
        pids = _pids(slow_cli)
        assert not any(_alive(pid) for pid in pids)


def test_cancelling_a_cli_chat_kills_the_cli_and_its_grandchild(
    slow_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> tuple[int, int]:
        backend = CodexCliBackend()
        task = asyncio.create_task(backend.open_chat(dict(CHAT), stream=False))
        pids = await _wait_for_pids_async(slow_cli)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()
        return pids

    pids = asyncio.run(scenario())
    _assert_gone(pids)


def test_cancelling_when_the_cli_ignores_sigterm_still_kills_it(
    slow_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_SLOW_IGNORE_TERM", "1")

    async def scenario() -> tuple[int, int]:
        backend = CodexCliBackend()
        task = asyncio.create_task(backend.open_chat(dict(CHAT), stream=False))
        pids = await _wait_for_pids_async(slow_cli)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()
        return pids

    _assert_gone(asyncio.run(scenario()))


def test_a_cli_timeout_on_the_chat_path_kills_the_group(
    slow_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing GatewayError path (cli_timeout) now escalates and reaps too."""
    monkeypatch.setenv("OS3_SLOW_IGNORE_TERM", "1")
    binary = os.environ["OS3_BRIDGE_CODEX_BIN"]

    async def scenario() -> None:
        with pytest.raises(Exception) as caught:
            await cli_exec.run_async(
                [binary, "exec"],
                stdin="",
                timeout=1.0,
                env=cli_exec.child_env(binary),
                cwd=None,
            )
        assert getattr(caught.value, "code", "") == "cli_timeout"
        await _settle()

    asyncio.run(scenario())
    _assert_gone(_pids(slow_cli))


def test_a_finished_cli_is_left_alone(tmp_path: Path) -> None:
    """kill() on a process that already exited must not signal its old group."""
    script = tmp_path / "quick"
    script.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    script.chmod(0o755)

    async def scenario() -> tuple[int, str]:
        result = await cli_exec.run_async(
            [str(script)], stdin="", timeout=5.0, env=dict(os.environ), cwd=None
        )
        return result.returncode, result.stdout

    assert asyncio.run(scenario()) == (0, "done\n")


def test_kill_survives_a_second_cancellation(
    slow_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGKILL goes out even when the cleanup itself is cancelled mid-grace."""
    monkeypatch.setenv("OS3_SLOW_IGNORE_TERM", "1")
    binary = os.environ["OS3_BRIDGE_CODEX_BIN"]

    async def scenario() -> tuple[int, int]:
        proc = await cli_exec.start(
            [binary, "exec"], stdin="", timeout=30.0, env=cli_exec.child_env(binary), cwd=None
        )
        pids = await _wait_for_pids_async(slow_cli)
        killer = asyncio.create_task(proc.kill(grace=10.0))
        await asyncio.sleep(0.2)
        killer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await killer
        await _settle()
        return pids

    _assert_gone(asyncio.run(scenario()))
