"""doctor.sh reports status and does not echo key material."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEALTH_INTERVAL = 0.5
HEALTH_ATTEMPTS = 5


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _doctor_env(tmp_path: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env["OS3_BRIDGE_KEY_FILE"] = str(tmp_path / "bridge.key")
    env["OS3_BRIDGE_OPENROUTER_KEY_FILE"] = str(tmp_path / "missing.key")
    env["OS3_BRIDGE_DOCTOR_SKIP_SMOKE"] = "1"
    env["OS3_BRIDGE_PORT"] = str(port)
    return env


def _run_doctor(
    env: dict[str, str], timeout: int = 120
) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(ROOT / "doctor.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=timeout,
    )
    return result, time.monotonic() - started


def test_doctor_sh_does_not_print_secrets(tmp_path: Path) -> None:
    upstream = "[REDACTED:openai_key]"
    bridge = "os3_bridge_doctor_secret_value"
    key_path = tmp_path / "bridge.key"
    key_path.write_text(bridge + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["OPENROUTER_API_KEY"] = upstream
    env["OS3_BRIDGE_KEY_FILE"] = str(key_path)
    env["OS3_BRIDGE_OPENROUTER_KEY_FILE"] = str(tmp_path / "missing.key")
    env["OS3_BRIDGE_DOCTOR_SKIP_SMOKE"] = "1"
    env["OS3_BRIDGE_DOCTOR_SKIP_CRED"] = "1"
    env["OS3_BRIDGE_BACKEND"] = "openrouter"
    env["OS3_BRIDGE_CODEX_BIN"] = str(tmp_path / "missing-codex")
    env["OS3_BRIDGE_CLAUDE_BIN"] = str(tmp_path / "missing-claude")
    env["OS3_BRIDGE_COPILOT_BIN"] = str(tmp_path / "missing-copilot")
    env["OS3_BRIDGE_GROK_BIN"] = str(tmp_path / "missing-grok")
    env["OS3_BRIDGE_AGY_BIN"] = str(tmp_path / "missing-agy")
    result = subprocess.run(
        ["bash", str(ROOT / "doctor.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert upstream not in combined
    assert bridge not in combined
    assert "codex_required" in result.stdout
    assert "smoke skipped" in result.stdout
    assert "Authorization" not in result.stdout
    assert upstream not in (ROOT / "doctor.sh").read_text(encoding="utf-8")


def test_doctor_waits_for_the_door_to_bind(tmp_path: Path) -> None:
    """A door that binds late still reads clean: `start` returns before it listens."""
    port = _free_port()
    env = _doctor_env(tmp_path, port)
    stand_in = tmp_path / "late_door.py"
    stand_in.write_text(
        "\n".join(
            [
                "import http.server",
                "import socketserver",
                "import sys",
                "",
                "",
                "class Server(http.server.HTTPServer):",
                "    # HTTPServer.server_bind does a reverse lookup between bind and listen.",
                "    # On macOS that stalls, and a bound-but-not-listening port drops SYNs",
                "    # instead of refusing them, so the door looks like a connect timeout.",
                "    def server_bind(self):",
                "        socketserver.TCPServer.server_bind(self)",
                "        self.server_name = '127.0.0.1'",
                "        self.server_port = self.server_address[1]",
                "",
                "",
                "class Handler(http.server.BaseHTTPRequestHandler):",
                "    def do_GET(self):",
                "        if self.path == '/health':",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.end_headers()",
                "            self.wfile.write(b'{\"ok\": true}')",
                "            return",
                "        self.send_response(404)",
                "        self.end_headers()",
                "",
                "    def log_message(self, *args):",
                "        pass",
                "",
                "",
                "Server(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    opened = threading.Event()
    lines: list[str] = []

    def read_doctor() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("-- wait /health"):
                opened.set()
            elif line.startswith("smoke skipped"):
                break

    # A named pipe rather than an extra descriptor: `bash -c` closes descriptors
    # above 2, so a hand-rolled fd 3 gate in that script is already closed and
    # releases the stand-in immediately. A fifo is a path, and survives.
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    script = "\n".join(
        [
            "set -uo pipefail",
            f"cd {ROOT}",
            f"( read -r gate <{gate}; exec python3 {stand_in} {port} ) >/dev/null 2>&1 &",
            "server=$!",
            "bash doctor.sh",
            "code=$?",
            "kill $server 2>/dev/null || true",
            "wait $server 2>/dev/null || true",
            "exit $code",
        ]
    )
    started = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-c", script],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    reader = threading.Thread(target=read_doctor)
    reader.start()
    assert opened.wait(60), "doctor never opened its /health wait"
    gate_at = time.monotonic() - started
    time.sleep(2 * HEALTH_INTERVAL)
    with open(gate, "w", encoding="utf-8") as handle:
        handle.write("bind now\n")
    bound_at = time.monotonic() - started
    proc.wait(timeout=120)
    reader.join()
    assert proc.stdout is not None
    proc.stdout.close()

    combined = "".join(lines)
    attempts = re.findall(r"^attempt (\d+)/(\d+): (.+)$", combined, re.MULTILINE)
    assert proc.returncode == 0, combined
    assert attempts, combined
    assert attempts[0][:2] == ("1", str(HEALTH_ATTEMPTS)), combined
    assert attempts[-1][2] == "HTTP 200", combined
    assert int(attempts[-1][0]) > 1, combined
    assert "door is answering" in combined, combined
    assert gate_at < (HEALTH_ATTEMPTS - 1) * HEALTH_INTERVAL, combined
    assert bound_at < (HEALTH_ATTEMPTS - 1) * HEALTH_INTERVAL, combined


def test_doctor_reports_the_real_state_when_the_door_never_binds(tmp_path: Path) -> None:
    port = _free_port()
    env = _doctor_env(tmp_path, port)
    env["OS3_BRIDGE_DOCTOR_HEALTH_ATTEMPTS"] = "2"
    env["OS3_BRIDGE_DOCTOR_HEALTH_INTERVAL"] = "0.1"
    result, elapsed = _run_doctor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "attempt 1/2" in result.stdout
    assert "attempt 2/2" in result.stdout
    assert "no /health answer after 2 attempts" in result.stdout
    assert "the live check below reports the real state" in result.stdout
    assert elapsed < 30, "the retry wait must stay bounded"
