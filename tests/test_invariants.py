"""Repository rules that agents broke before, as checks. AGENTS.md pairs each with its rule."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from nexus.app import main
from nexus.config import DEFAULT_HOST, DEFAULT_PORT, loopback_host

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "nexus"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _tracked(*pathspecs: str) -> list[Path]:
    names = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [ROOT / name for name in names if name and (ROOT / name).is_file()]


def _source_files() -> list[Path]:
    return sorted(_tracked(":(glob)src/nexus/**/*.py"))


PORT_LITERAL_HOMES = {
    "src/nexus/config.py": "DEFAULT_PORT is defined here",
    "install.sh": "shell default, pinned by test_shell_and_dev_server_defaults_match_the_config",
    "ui/vite.config.ts": "dev proxy default, pinned by the same test",
    ".env.example": "documents the default",
    "pyproject.toml": "package description",
}
_SKIPPED_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "tests",
    "docs",
    "assets",
    "ISSUE_TEMPLATE",
    "__pycache__",
}
_SKIPPED_SUFFIXES = {".md", ".woff2", ".png", ".jpg", ".svg", ".ico", ".pyc"}


def _scanned_files() -> list[Path]:
    out = []
    for path in _tracked():
        parts = set(path.relative_to(ROOT).parts)
        if parts & _SKIPPED_DIRS or path.suffix in _SKIPPED_SUFFIXES:
            continue
        if path.name in {"package-lock.json"} or path.name.startswith(".ruff"):
            continue
        out.append(path)
    return out


def port_literal_offenders(root: Path = ROOT, files: list[Path] | None = None) -> list[str]:
    found = []
    for path in files if files is not None else _scanned_files():
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        if str(DEFAULT_PORT) in text and rel not in PORT_LITERAL_HOMES:
            found.append(rel)
    return found


def test_the_default_port_literal_lives_in_config_only() -> None:
    offenders = port_literal_offenders()
    assert not offenders, (
        f"{offenders} spell the default port {DEFAULT_PORT}. Build the address from "
        "nexus.config.configured_port() / door_origin(), or read NEXUS_PORT, so a custom "
        "port is never contradicted."
    )


def test_default_port_constant_is_not_used_outside_config() -> None:
    users = [
        path.relative_to(ROOT).as_posix()
        for path in _source_files()
        if path.name != "config.py" and "DEFAULT_PORT" in path.read_text(encoding="utf-8")
    ]
    assert not users, f"{users} use DEFAULT_PORT; use configured_port(), which honours NEXUS_PORT"


def test_shell_and_dev_server_defaults_match_the_config() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    vite = (ROOT / "ui" / "vite.config.ts").read_text(encoding="utf-8")
    assert re.search(rf"DOOR_PORT={DEFAULT_PORT}\b", install)
    assert f"127.0.0.1:{DEFAULT_PORT}" in vite


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "::1", "*", "10.0.0.5", "192.168.1.20", "example.com", "0:0:0:0:0:0:0:0"],
)
def test_main_refuses_every_non_loopback_host(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("NEXUS_HOST", host)
    with patch("nexus.app.uvicorn.run") as run:
        with pytest.raises(SystemExit):
            main()
    run.assert_not_called()


def test_the_default_bind_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NEXUS_HOST", "OS3_BRIDGE_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert DEFAULT_HOST == loopback_host() == "127.0.0.1"
    with patch("nexus.app.uvicorn.run") as run:
        main()
    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_uvicorn_starts_in_one_place_with_the_loopback_host() -> None:
    starts = []
    for path in _source_files():
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn"
            ):
                starts.append((path.name, ast.unparse(dict_of(node)["host"])))
    assert starts == [("app.py", "host")]
    main_source = ast.unparse(next(n for n in _parse(SRC / "app.py").body if _is_def(n, "main")))
    assert "host = loopback_host()" in main_source


def test_no_wildcard_bind_address_is_written_anywhere_in_the_package_or_units() -> None:
    wide = "0.0.0" + ".0"
    files = [*_source_files(), *_tracked("packaging")]
    assert [p.name for p in files if p.is_file() and wide in p.read_text(errors="ignore")] == []


def dict_of(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _is_def(node: ast.stmt, name: str) -> bool:
    return isinstance(node, ast.FunctionDef) and node.name == name


def contextvar_writers(pulse: ast.Module) -> set[str]:
    """Functions in pulse.py that set a module-level ContextVar, except the box-backed one."""
    names = {
        t.id
        for node in pulse.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        for t in [node.target]
        if "ContextVar" in ast.unparse(node.value or node)
    }
    writers = set()
    for node in pulse.body:
        if not isinstance(node, ast.FunctionDef) or node.name == "note_tokens":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "set"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in names
            ):
                writers.add(node.name)
    return writers


def _own_nodes(func: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))
    return out


def contextvar_writes_in_generators(tree: ast.Module, writers: set[str]) -> list[str]:
    bad = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        own = _own_nodes(func)
        if not any(isinstance(n, ast.Yield | ast.YieldFrom) for n in own):
            continue
        for node in own:
            if isinstance(node, ast.Call):
                callee = node.func
                name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
                if name in writers:
                    bad.append(f"{func.name} calls {name}")
    return bad


def test_no_context_var_write_inside_a_stream_generator() -> None:
    writers = contextvar_writers(_parse(SRC / "pulse.py"))
    assert {"note_model", "note_backend", "note_error", "note_usage_limit"} <= writers
    offenders = {
        path.name: bad
        for path in _source_files()
        if (bad := contextvar_writes_in_generators(_parse(path), writers))
    }
    assert not offenders, (
        f"{offenders}: a streamed body runs in a child task, so a ContextVar set there never "
        "reaches the request log. Set it before the generator starts, or report through "
        "pulse.note_tokens, which fills the per-request box."
    )


STREAM_GENERATOR_THAT_LOSES_TOKENS = """
async def guarded(upstream):
    async for chunk in upstream:
        yield chunk
    note_usage_limit()
"""


def test_the_generator_guard_flags_the_original_mistake() -> None:
    tree = ast.parse(STREAM_GENERATOR_THAT_LOSES_TOKENS)
    assert contextvar_writes_in_generators(tree, {"note_usage_limit"}) == [
        "guarded calls note_usage_limit"
    ]


SENSITIVE = re.compile(
    r"(?<![a-z])(api_?key|key|token|secret|password|bearer|authorization|credentials?)s?(?![a-z])"
    r"|_key$|_token$|_secret$",
    re.I,
)
NOT_A_VALUE = re.compile(r"(_path|_file|_source|_present|_env|_name)$")
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _is_sink(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        base = func.value
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        return "log" in name.lower()
    return False


def _unredacted_names(expr: ast.expr) -> list[str]:
    names: list[str] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "redact") or (
                isinstance(func, ast.Attribute) and func.attr == "redact"
            ):
                return
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(expr)
    return [n for n in names if SENSITIVE.search(n) and not NOT_A_VALUE.search(n)]


def unredacted_secret_output(tree: ast.Module) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_sink(node):
            args = [*node.args, *(kw.value for kw in node.keywords)]
            for name in (n for arg in args for n in _unredacted_names(arg)):
                found.append(f"line {node.lineno}: {name} -> {ast.unparse(node)[:80]}")
    return found


def test_nothing_secret_is_logged_or_printed_unredacted() -> None:
    offenders = {
        path.name: bad
        for path in [*_source_files(), ROOT / "server.py", *_tracked(":(glob)scripts/*.py")]
        if (bad := unredacted_secret_output(_parse(path)))
    }
    assert not offenders, (
        f"{offenders}: pass anything named like a key, token or secret through "
        "nexus.redact.redact() before it reaches print() or a logger, or log its path/presence."
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'print(f"token: {access_token}")',
        'logger.info("using key %s", api_key)',
        'log.warning("auth " + bearer)',
        "print(creds.access_token)",
    ],
)
def test_the_secret_guard_flags_the_leak_shapes(snippet: str) -> None:
    assert unredacted_secret_output(ast.parse(snippet))


@pytest.mark.parametrize(
    "snippet",
    [
        'print(f"token: {redact(access_token)}")',
        'logger.info("key file %s", key_path)',
        'print(f"key present: {key_present}")',
    ],
)
def test_the_secret_guard_allows_redacted_and_path_output(snippet: str) -> None:
    assert not unredacted_secret_output(ast.parse(snippet))


PINNED_TEMPLATES = {
    "packaging/launchd/ai.nexus.server.plist.template": (
        "48b2f43a8a537d10a34889be51fd7cfa068a1520288da2ddcf03d210b37e3ffc"
    ),
    "packaging/systemd/nexus.service": (
        "5b18220833f22c70c0ee1cf032ecf24f7248ccafcfdb662e96ca29020596a890"
    ),
}


@pytest.mark.parametrize("relative", sorted(PINNED_TEMPLATES))
def test_installed_unit_templates_are_unchanged_without_intent(relative: str) -> None:
    digest = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    assert digest == PINNED_TEMPLATES[relative], (
        f"{relative} changed (sha256 {digest}). Machines that ran `./nexus start` keep the old "
        "unit, and it runs `python -m os3_bridge.cli run` under the label "
        "com.grokbot.os3-bridge or os3-bridge.service, so a template edit must still work with "
        "an already-installed unit. If the change is intended, update PINNED_TEMPLATES in "
        "tests/test_invariants.py in the same commit and say why in the commit message."
    )


def node_majors(ci_text: str) -> set[str]:
    majors = set(re.findall(r"node-version:\s*[\"']?(\d+)", ci_text))
    for pointer in re.findall(r"node-version-file:\s*[\"']?([^\s\"']+)", ci_text):
        majors.add(re.match(r"\d+", (ROOT / pointer).read_text(encoding="utf-8").strip())[0])
    return majors


def test_node_major_agrees_across_nvmrc_engines_and_ci() -> None:
    nvmrc = re.match(r"\d+", (ROOT / "ui" / ".nvmrc").read_text(encoding="utf-8").strip())[0]
    engines = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))["engines"]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert nvmrc == "22"
    assert set(re.findall(r"\d+", engines["node"])) >= {nvmrc}
    assert node_majors(ci) == {nvmrc}, (
        f"ui/.nvmrc says {nvmrc}; .github/workflows/ci.yml sets {node_majors(ci)}. "
        "Vitest and the build must run on the same Node everywhere."
    )
    assert f'@types/node": "^{nvmrc}.' in (ROOT / "ui" / "package.json").read_text()


def test_node_check_flags_the_old_split() -> None:
    assert node_majors('node-version: "20"') == {"20"}


def named_checks(agents_text: str) -> list[tuple[str, str | None]]:
    found = []
    for row in agents_text.splitlines():
        if not row.startswith("| ") or row.startswith("| Rule") or row.startswith("| ---"):
            continue
        current = ""
        for token in re.findall(r"`([^`]+)`", row.split("|")[2]):
            head, _, name = token.partition("::")
            if head:
                current = head
            if current.startswith(("tests/", "scripts/")):
                found.append((current, name or None))
    return found


def test_every_check_named_in_agents_md_exists() -> None:
    checks = named_checks((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    assert len(checks) >= 15
    missing = []
    for relative, name in checks:
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
        elif name and path.suffix == ".py":
            defined = {n.name for n in ast.walk(_parse(path)) if isinstance(n, ast.FunctionDef)}
            if name not in defined:
                missing.append(f"{relative}::{name}")
    assert not missing, f"AGENTS.md names checks that do not exist: {missing}"
