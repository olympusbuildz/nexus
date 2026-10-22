"""local-ci.sh checks a commit in a throwaway worktree and can post a status."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "local-ci.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]

OK_MAKEFILE = """\
.PHONY: dev-install check
dev-install:
\t@echo dev-install
check:
\t@echo "3 passed in 0.01s"
"""

FAIL_MAKEFILE = """\
.PHONY: dev-install check
dev-install:
\t@echo dev-install
check:
\t@echo "1 failed, 2 passed in 0.02s"
\t@exit 1
"""

UV_STUB = """\
#!/bin/sh
if [ "${1:-}" = "venv" ]; then
  dest=${2:?}
  mkdir -p "$dest/bin"
  printf '%s\\n' '#!/bin/sh' 'exit 0' > "$dest/bin/python"
  chmod +x "$dest/bin/python"
fi
exit 0
"""

GH_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$GH_CALLS"
exit 1
"""


def _repo(path: Path, makefile: str) -> Path:
    path.mkdir(parents=True)
    (path / "Makefile").write_text(makefile, encoding="utf-8")
    subprocess.run([*GIT, "init", "-q"], cwd=path, check=True)
    subprocess.run([*GIT, "add", "Makefile"], cwd=path, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=path, check=True)
    (path / "untracked-runtime.txt").write_text("live\n", encoding="utf-8")
    return path


def _install(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run(repo: Path, bin_dir: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
    env.update(extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def _worktree_paths(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    prefix = "worktree "
    return [line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix)]


def test_no_post_reports_parsed_summary_and_removes_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "nexus", OK_MAKEFILE)
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "gh-calls.log"
    _install(bin_dir, "uv", UV_STUB)
    _install(bin_dir, "gh", GH_STUB)
    result = _run(repo, bin_dir, "--no-post", GH_CALLS=str(calls))
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "local-ci: success: 3 passed" in result.stdout
    assert "in 0.01s" not in result.stdout.split("local-ci: success: ", 1)[1].splitlines()[0]
    assert [Path(p).resolve() for p in _worktree_paths(repo)] == [repo.resolve()]
    assert (repo / "untracked-runtime.txt").read_text(encoding="utf-8") == "live\n"
    assert not calls.exists()
    log_lines = [line for line in result.stdout.splitlines() if line.startswith("log: ")]
    assert log_lines
    assert Path(log_lines[0].split(" ", 1)[1]).is_file()


def test_failing_check_is_nonzero_and_cleans_up(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "nexus", FAIL_MAKEFILE)
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "uv", UV_STUB)
    result = _run(repo, bin_dir, "--no-post", "HEAD")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "local-ci: failure: 1 failed, 2 passed" in result.stdout
    assert [Path(p).resolve() for p in _worktree_paths(repo)] == [repo.resolve()]
    assert (repo / "untracked-runtime.txt").read_text(encoding="utf-8") == "live\n"


def test_missing_gh_auth_exits_without_posting(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "nexus", OK_MAKEFILE)
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "gh-calls.log"
    _install(bin_dir, "uv", UV_STUB)
    _install(bin_dir, "gh", GH_STUB)
    result = _run(repo, bin_dir, GH_CALLS=str(calls))
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "gh is not authenticated; run gh auth login (or use --no-post)" in combined
    logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "auth status" in logged
    assert "api" not in logged
    assert [Path(p).resolve() for p in _worktree_paths(repo)] == [repo.resolve()]
