"""Find the key file that puts a non-Local OS3 connection back, without the user naming it.

OS3 reports only a masked key (``sk-or-v1-****wxyz``). When ``NEXUS_RABBIT_RESTORE_KEY_FILE``
is unset, the key dirs from ``config.rabbit_key_dirs`` are searched for exactly one key file
that fits that mask. Auto-detect needs a mask that shows at least ``MIN_PREFIX`` characters
before the ``****`` and ``MIN_SUFFIX`` after it; a shorter mask picks nothing. Zero matches, or
two or more, never guess.

Every key file, found or named in the env var, is read by ``read_key_file`` and only by it,
at match time and at restore time alike: a regular file of at most 4 KB, opened without
following a symlink, with no group or other permission bits, that is not the bridge's own key
(by file identity), and that holds exactly one key on one line. Nothing here returns, logs or
stores a key: a value lives in this module only long enough to be compared or sent to OS3,
and results carry file paths.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.config import bridge_key_path, rabbit_key_dirs, rabbit_restore_key_file

MAX_KEY_FILE_BYTES = 4096
MIN_PREFIX = 3
MIN_SUFFIX = 4
MAX_LISTED = 5
_MASK = re.compile(r"([^*]*)\*{4,}([^*]*)")
_NOT_OWNER_ONLY = stat.S_IRWXG | stat.S_IRWXO
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class KeyFileError(Exception):
    """Why a file cannot be used as a key file. The message names the path, never a key."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def mask_parts(masked: str | None) -> tuple[str, str] | None:
    """``(visible prefix, visible suffix)`` of ``prefix****suffix``, else None."""
    if not isinstance(masked, str):
        return None
    found = _MASK.fullmatch(masked)
    return (found.group(1), found.group(2)) if found else None


def mask_is_specific(masked: str | None) -> bool:
    """True when the mask shows enough of the key for auto-detect to pick a file."""
    parts = mask_parts(masked)
    return parts is not None and len(parts[0]) >= MIN_PREFIX and len(parts[1]) >= MIN_SUFFIX


def masked_key_matches(masked: str | None, key: str, *, min_prefix: int = MIN_PREFIX) -> bool:
    """True when ``key`` fits ``prefix****suffix`` and the mask shows enough of it.

    The visible suffix must be at least ``MIN_SUFFIX`` characters and the visible prefix at
    least ``min_prefix``; both must match. ``min_prefix=0`` is for a key the user named or the
    bridge's own key, where OS3's mask may show no prefix.
    """
    parts = mask_parts(masked)
    if parts is None or not key:
        return False
    prefix, suffix = parts
    return (
        len(prefix) >= min_prefix
        and len(suffix) >= MIN_SUFFIX
        and len(key) > len(prefix) + len(suffix)
        and key.startswith(prefix)
        and key.endswith(suffix)
    )


def _bridge_key_identity() -> tuple[int, int] | None:
    try:
        own = os.stat(bridge_key_path())
    except OSError:
        return None
    return own.st_dev, own.st_ino


def _open_key_file(path: Path) -> tuple[int, os.stat_result]:
    """Open ``path`` as a key file without reading it, or raise KeyFileError."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP or os.path.islink(path):
            raise KeyFileError("symlink", f"{path} is a symlink, which is not followed") from None
        raise KeyFileError("unopenable", f"cannot open {path}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KeyFileError("not_regular", f"{path} is not a regular file")
        if info.st_size > MAX_KEY_FILE_BYTES:
            raise KeyFileError("too_large", f"{path} is larger than {MAX_KEY_FILE_BYTES} bytes")
        if info.st_mode & _NOT_OWNER_ONLY:
            raise KeyFileError(
                "not_owner_only",
                f"{path} has mode {stat.S_IMODE(info.st_mode):o}; a key file must be "
                "owner-only (chmod 600)",
            )
        if (info.st_dev, info.st_ino) == _bridge_key_identity():
            raise KeyFileError("bridge_key", f"{path} is the bridge's own key file")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _read_one_key(fd: int, path: Path) -> str:
    """The single key in an opened key file, or KeyFileError. Surrounding whitespace is dropped."""
    data = b""
    while len(data) <= MAX_KEY_FILE_BYTES:
        chunk = os.read(fd, MAX_KEY_FILE_BYTES + 1 - len(data))
        if not chunk:
            break
        data += chunk
    if len(data) > MAX_KEY_FILE_BYTES:
        raise KeyFileError("too_large", f"{path} is larger than {MAX_KEY_FILE_BYTES} bytes")
    try:
        key = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        key = ""
    if not key or not key.isprintable() or any(ch.isspace() for ch in key):
        raise KeyFileError(
            "not_one_key", f"{path} must hold exactly one key on one line and nothing else"
        )
    return key


def read_key_file(path: Path) -> str:
    """The key in ``path`` under the key-file rules, or KeyFileError saying why not."""
    fd, _ = _open_key_file(path)
    try:
        return _read_one_key(fd, path)
    finally:
        os.close(fd)


@dataclass
class KeyScan:
    candidates: int = 0
    unreadable: list[Path] = field(default_factory=list)
    rejected: list[Path] = field(default_factory=list)
    matches: list[Path] = field(default_factory=list)


def _scan_entry(scan: KeyScan, path: Path, masked: str | None, seen: set[tuple[int, int]]) -> None:
    try:
        fd, info = _open_key_file(path)
    except KeyFileError as exc:
        if exc.reason == "not_owner_only":
            scan.unreadable.append(path)
        return
    try:
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            return
        seen.add(identity)
        scan.candidates += 1
        if masked is None:
            return
        try:
            key = _read_one_key(fd, path)
        except KeyFileError:
            scan.rejected.append(path)
            return
        if masked_key_matches(masked, key):
            scan.matches.append(path)
    except OSError:
        return
    finally:
        os.close(fd)


def scan_key_files(masked: str | None, dirs: list[Path] | None = None) -> KeyScan:
    """Walk the key dirs. With ``masked=None`` nothing is read, files are only counted."""
    scan = KeyScan()
    seen: set[tuple[int, int]] = set()
    for raw in rabbit_key_dirs() if dirs is None else dirs:
        directory = Path(os.path.realpath(raw))
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            _scan_entry(scan, directory / name, masked, seen)
    return scan


def _listed(paths: list[Path]) -> str:
    shown = ", ".join(str(p) for p in paths[:MAX_LISTED])
    extra = len(paths) - MAX_LISTED
    return shown + (f" (and {extra} more)" if extra > 0 else "")


@dataclass
class RestoreKey:
    """Which file, if any, puts the current non-Local connection back."""

    source: str
    path: Path | None = None
    matches: list[Path] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)
    rejected: list[Path] = field(default_factory=list)
    mask_too_short: bool = False
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return self.path is not None and self.problem is None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "path": str(self.path) if self.path else None,
        }
        if self.source == "ambiguous":
            out["paths"] = [str(p) for p in self.matches]
        if self.unreadable:
            out["skipped_unreadable"] = [str(p) for p in self.unreadable]
        if self.rejected:
            out["skipped_not_one_key"] = [str(p) for p in self.rejected]
        if self.mask_too_short:
            out["mask_too_short"] = True
        if self.problem:
            out["problem"] = self.problem
        return out

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if self.problem:
            notes.append(
                f"NEXUS_RABBIT_RESTORE_KEY_FILE cannot put that connection back: {self.problem}."
            )
        if self.mask_too_short:
            notes.append(
                "OS3's masked key shows too little of the key to pick a key file safely "
                f"(auto-detect needs at least {MIN_PREFIX} characters before the **** and "
                f"{MIN_SUFFIX} after it), so none was picked. Set "
                "NEXUS_RABBIT_RESTORE_KEY_FILE to the right key file."
            )
        if self.source == "ambiguous":
            notes.append(
                "More than one key file matches the key OS3 reports, so none was picked: "
                f"{_listed(self.matches)}. Set NEXUS_RABBIT_RESTORE_KEY_FILE to the right one."
            )
        if self.unreadable:
            notes.append(
                f"Skipped {len(self.unreadable)} file(s) in the key dirs that are not owner-only, "
                f"without reading them: {_listed(self.unreadable)}. "
                "chmod 600 a key file to make it a candidate."
            )
        if self.rejected:
            notes.append(
                f"Skipped {len(self.rejected)} file(s) in the key dirs that do not hold exactly "
                f"one key on one line: {_listed(self.rejected)}. A key file holds the key and "
                "nothing else, no comment or second line."
            )
        return notes


def _explicit_problem(path: Path, masked: str | None) -> str | None:
    try:
        key = read_key_file(path)
    except KeyFileError as exc:
        return str(exc)
    if not masked_key_matches(masked, key, min_prefix=0):
        return f"{path} does not match the key OS3 reports"
    return None


def find_restore_key(masked: str | None) -> RestoreKey:
    """The restore key file: the env var when set, else the one file that fits ``masked``."""
    explicit = rabbit_restore_key_file()
    if explicit is not None:
        return RestoreKey("env", explicit, problem=_explicit_problem(explicit, masked))
    if not mask_is_specific(masked):
        return RestoreKey("none", mask_too_short=True)
    scan = scan_key_files(masked)
    found = {"unreadable": scan.unreadable, "rejected": scan.rejected}
    if len(scan.matches) == 1:
        return RestoreKey("auto-detected", scan.matches[0], **found)
    source = "ambiguous" if scan.matches else "none"
    return RestoreKey(source, matches=scan.matches, **found)


def describe_candidates() -> dict[str, Any]:
    """For ``plan``, which cannot know the masked key: how many files could be candidates."""
    explicit = rabbit_restore_key_file()
    if explicit is not None:
        return {"source": "env", "path": str(explicit)}
    scan = scan_key_files(None)
    return {
        "source": "auto",
        "candidates": scan.candidates,
        "skipped_unreadable": len(scan.unreadable),
        "dirs": [str(p) for p in rabbit_key_dirs()],
    }
