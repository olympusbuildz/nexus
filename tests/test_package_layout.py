"""The package lives in src/nexus; os3_bridge and ./os3-bridge stay as compat names."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBCOMMANDS = "{setup,start,stop,status,run,test,doctor,menubar,rabbit}"


def _env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(extra)
    return env


def _python(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_import_os3_bridge_aliases_nexus() -> None:
    script = (
        "import os3_bridge, nexus\n"
        "import os3_bridge.config, nexus.config\n"
        "import os3_bridge.backends.registry, nexus.backends.registry\n"
        "from os3_bridge.cli import main\n"
        "import nexus.cli\n"
        "assert os3_bridge is nexus\n"
        "assert os3_bridge.config is nexus.config\n"
        "assert os3_bridge.backends.registry is nexus.backends.registry\n"
        "assert main is nexus.cli.main\n"
        "assert nexus.config.__spec__.name == 'nexus.config'\n"
        "print(nexus.config.repo_root())\n"
    )
    result = _python("-c", script, env=_env())
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == ROOT


def test_python_m_os3_bridge_cli_help() -> None:
    result = _python("-m", "os3_bridge.cli", "--help", env=_env())
    assert result.returncode == 0, result.stderr
    assert SUBCOMMANDS in result.stdout


def test_python_m_nexus_cli_help() -> None:
    result = _python("-m", "nexus.cli", "--help", env=_env(PYTHONPATH=str(ROOT / "src")))
    assert result.returncode == 0, result.stderr
    assert SUBCOMMANDS in result.stdout


def test_nexus_and_os3_bridge_wrappers_run_the_same_cli(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    python = checkout / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    (checkout / "src").symlink_to(ROOT / "src")
    outputs = []
    for name in ("nexus", "os3-bridge"):
        wrapper = ROOT / name
        assert os.access(wrapper, os.X_OK)
        (checkout / name).write_bytes(wrapper.read_bytes())
        (checkout / name).chmod(0o755)
        result = subprocess.run(
            [str(checkout / name), "--help"],
            cwd=tmp_path,
            env=_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert SUBCOMMANDS in outputs[0]
    assert outputs[0] == outputs[1]


def test_console_scripts_point_at_nexus_cli() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["nexus"] == "nexus.cli:main"
    assert scripts["os3-bridge"] == "nexus.cli:main"
