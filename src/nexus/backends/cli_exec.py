"""Start an official CLI. No shell, and no credential files are opened here.

The child environment drops provider API keys so a subscription CLI is not
switched onto API billing by variables set for the key-based backends.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from nexus.config import environ_get
from nexus.errors import GatewayError

# Present in the parent when another backend is configured. A subscription CLI
# that sees these uses API billing instead of the user's existing login.
_SCRUBBED_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CODEX_API_KEY",
        "GEMINI_API_KEY",
        "NOUS_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    }
)


class CliSpawnError(Exception):
    """The binary was not started."""


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str
    started: bool


# launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin. Homebrew installs
# live in these prefixes, so a probe can still see them when PATH misses.
BREW_BIN_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")


def grok_bin_dir() -> Path:
    """Grok Build installs ``grok`` in the user home, not under Homebrew."""
    return Path.home() / ".grok" / "bin"


def _executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_binary(env_name: str, command: str) -> str | None:
    """Configured path wins. A missing override does not fall through to PATH."""
    override = (environ_get(env_name) or "").strip()
    if override:
        path = Path(override).expanduser()
        if _executable(path):
            return str(path)
        return None
    found = shutil.which(command)
    if found:
        return found
    if not command or "/" in command or "\\" in command:
        return None
    for prefix in (*BREW_BIN_PREFIXES, str(grok_bin_dir())):
        candidate = Path(prefix) / command
        if _executable(candidate):
            return str(candidate)
    return None


def child_env(binary: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
    bin_dir = str(Path(binary).resolve().parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def _text(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def run_sync(
    argv: list[str],
    *,
    stdin: str,
    timeout: float,
    env: dict[str, str],
    cwd: str | None,
) -> CliResult:
    """Blocking probe. Timeout means the process was started."""
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        return CliResult(124, _text(exc.stdout), _text(exc.stderr) or "probe timed out", True)
    except FileNotFoundError as exc:
        raise CliSpawnError(exc.strerror or "not found") from None
    except OSError as exc:
        raise CliSpawnError(exc.strerror or "spawn failed") from None
    return CliResult(completed.returncode, completed.stdout, completed.stderr, True)


KILL_GRACE_S = 0.5
_POLL_S = 0.05
_REAP_S = 2.0


def _signal_group(pgid: int, sig: int) -> None:
    """Signal a process group. Gone or not ours is not an error."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class CliProcess:
    """One CLI process. Stdout is iterated while stdin is fed, so neither pipe fills."""

    def __init__(self, proc: asyncio.subprocess.Process, *, timeout: float) -> None:
        self.proc = proc
        self.timeout = timeout
        self._stderr: asyncio.Task[bytes] | None = None
        self._feed: asyncio.Task[None] | None = None
        self._deadline = asyncio.get_running_loop().time() + timeout
        if proc.stderr is not None:
            self._stderr = asyncio.create_task(proc.stderr.read())

    def feed(self, data: bytes) -> None:
        self._feed = asyncio.create_task(_write_stdin(self.proc, data))

    async def lines(self) -> AsyncIterator[str]:
        stdout = self.proc.stdout
        if stdout is None:
            return
        while True:
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self.kill()
                raise GatewayError(
                    504,
                    "CLI timed out",
                    "api_error",
                    "cli_timeout",
                )
            try:
                line = await asyncio.wait_for(stdout.readline(), remaining)
            except TimeoutError:
                await self.kill()
                raise GatewayError(
                    504,
                    "CLI timed out",
                    "api_error",
                    "cli_timeout",
                ) from None
            if not line:
                break
            yield line.decode("utf-8", errors="replace")

    async def finish(self) -> tuple[int, str]:
        if self._feed is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._feed
        stderr = ""
        if self._stderr is not None:
            try:
                raw = await asyncio.wait_for(self._stderr, max(0.1, self.timeout))
            except TimeoutError:
                self._stderr.cancel()
                raw = b""
            stderr = raw.decode("utf-8", errors="replace")
        code = self.proc.returncode
        if code is None:
            remaining = self._deadline - asyncio.get_running_loop().time()
            try:
                code = await asyncio.wait_for(self.proc.wait(), max(0.1, remaining))
            except TimeoutError:
                await self.kill()
                raise GatewayError(
                    504,
                    "CLI timed out",
                    "api_error",
                    "cli_timeout",
                ) from None
        return code, stderr

    async def kill(self, grace: float = KILL_GRACE_S) -> None:
        """Stop the CLI and everything it started, then reap it.

        The child leads its own session, so its pid is the process group id. SIGTERM goes
        to the group first, the group gets ``grace`` seconds to leave, and SIGKILL
        follows for whatever is left, including grandchildren after the leader has
        exited. The SIGKILL is sent even if this coroutine is itself cancelled while it
        waits.
        """
        proc = self.proc
        pgid = proc.pid if proc.returncode is None else 0
        try:
            if pgid:
                _signal_group(pgid, signal.SIGTERM)
                deadline = asyncio.get_running_loop().time() + grace
                while _group_alive(pgid) and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(_POLL_S)
        finally:
            if pgid:
                _signal_group(pgid, signal.SIGKILL)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            if self._feed is not None:
                self._feed.cancel()
            if self._stderr is not None and not self._stderr.done():
                self._stderr.cancel()
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
        with contextlib.suppress(ProcessLookupError, TimeoutError):
            await asyncio.wait_for(proc.wait(), _REAP_S)


async def start(
    argv: list[str],
    *,
    stdin: str,
    timeout: float,
    env: dict[str, str],
    cwd: str | None,
) -> CliProcess:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CliSpawnError(exc.strerror or "not found") from None
    except OSError as exc:
        raise CliSpawnError(exc.strerror or "spawn failed") from None
    running = CliProcess(proc, timeout=timeout)
    running.feed(stdin.encode("utf-8"))
    return running


async def _write_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        return
    try:
        proc.stdin.close()
        await proc.stdin.wait_closed()
    except (BrokenPipeError, RuntimeError):
        return


async def run_async(
    argv: list[str],
    *,
    stdin: str,
    timeout: float,
    env: dict[str, str],
    cwd: str | None,
) -> CliResult:
    proc = await start(argv, stdin=stdin, timeout=timeout, env=env, cwd=cwd)
    parts: list[str] = []
    try:
        async for line in proc.lines():
            parts.append(line)
        code, stderr = await proc.finish()
    except BaseException:
        await proc.kill()
        raise
    return CliResult(code, "".join(parts), stderr, True)
