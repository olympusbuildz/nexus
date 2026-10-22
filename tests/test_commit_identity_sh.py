"""scripts/check_commit_identity.sh: the commit identity and personal-mailbox gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_commit_identity.sh"
GOOD = "314062777+olympusbuildz@users.noreply.github.com"
PAST_MISTAKE = "cursoragent" + "@cursor.com"
PERSONAL = "someone" + "@" + "out" + "look.com"
OTHER_PERSONAL = "someone" + "@" + "gma" + "il.com"


def _git(repo: Path, *args: str, author: str = GOOD, committer: str = GOOD) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": author,
        "GIT_COMMITTER_NAME": "c",
        "GIT_COMMITTER_EMAIL": committer,
    }
    done = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("base\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(repo: Path, text: str = "next\n", **who: str) -> None:
    (repo / "a.txt").write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change", **who)


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "HEAD~1..HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )


def test_noreply_author_and_committer_pass(repo: Path) -> None:
    _commit(repo)
    done = _run(repo)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("who", "needle"),
    [
        ({"author": PAST_MISTAKE}, "author is"),
        ({"committer": PAST_MISTAKE}, "committer is"),
        ({"author": PERSONAL, "committer": PERSONAL}, "author is"),
    ],
    ids=["wrong author", "wrong committer", "personal mailbox identity"],
)
def test_any_other_identity_fails_with_the_fix(
    repo: Path, who: dict[str, str], needle: str
) -> None:
    _commit(repo, **who)
    done = _run(repo)
    assert done.returncode == 1
    assert needle in done.stderr
    assert "git config user.email" in done.stderr


@pytest.mark.parametrize("address", [PERSONAL, OTHER_PERSONAL], ids=["mailbox-a", "mailbox-b"])
def test_a_personal_address_in_the_tree_fails_without_echoing_it(repo: Path, address: str) -> None:
    _commit(repo, f"contact {address}\n")
    done = _run(repo)
    assert done.returncode == 1
    assert "a.txt:1" in done.stderr
    assert address not in done.stdout + done.stderr


def test_the_noreply_address_in_the_tree_is_fine(repo: Path) -> None:
    _commit(repo, f"Co-authored-by: x <{GOOD}>\n")
    assert _run(repo).returncode == 0


def test_the_repository_tree_has_no_personal_mailbox() -> None:
    done = subprocess.run(
        ["bash", str(SCRIPT), "HEAD^!"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert "personal mailbox" not in done.stderr, done.stderr
