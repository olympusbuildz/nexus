"""Guided setup. Writes key files mode 600 and prints OS3 CONNECT steps.

Subscription CLIs are offered only when the binary is on PATH. This module
does not start those CLIs and does not read their login files.
"""

from __future__ import annotations

import getpass
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nexus.auth import ensure_bridge_key
from nexus.backend_state import READY, base_state
from nexus.backends.anthropic import ANTHROPIC_VERSION
from nexus.backends.cli_exec import resolve_binary
from nexus.backends.registry import build_backend
from nexus.chatgpt_auth import login_status as chatgpt_login_status
from nexus.config import (
    ANTIGRAVITY_ENABLE_ENV,
    BACKEND_DEFAULT_MODELS,
    BACKEND_IDS,
    CLI_BIN_ENV,
    DEFAULT_HOST,
    active_backend_id,
    anthropic_base,
    anthropic_key_path,
    antigravity_enabled,
    backend_choice_from_file,
    backend_choice_path,
    bridge_key_path,
    canonical_backend,
    configured_port,
    environ_get,
    key_write_path,
    model_choice_from_file,
    model_choice_path,
    nous_base,
    nous_key_path,
    openai_base,
    openai_key_path,
    openrouter_base,
    openrouter_key_path,
    private_mode,
    valid_model_id,
    xai_base,
    xai_key_path,
)
from nexus.keys import load_key
from nexus.redact import redact
from nexus.service import dashboard_restart_advice


class SetupError(Exception):
    """User-facing setup failure. The message must not contain a secret.

    ``code`` is the stable identifier the dashboard envelope carries (docs/ui-api.md).
    The CLI prints only the message.
    """

    def __init__(
        self, message: str, code: str = "invalid_request", *, hint: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclass(frozen=True)
class ApiBackend:
    label: str
    key_path: Callable[[], Path]
    env_var: str
    # The cheapest endpoint that proves the credential: a key must be refused
    # here before it can fail anywhere else. OpenRouter's /models list is
    # public and answers 200 to a garbage key, so it checks /key instead.
    verify_url: Callable[[], str]

    def key_write_path(self) -> Path:
        """Where a saved key goes. ``NEXUS_<PROVIDER>_KEY_FILE`` is honoured as the override."""
        override = f"NEXUS_{self.env_var.removesuffix('_API_KEY')}_KEY_FILE"
        return key_write_path(self.key_path(), override)


@dataclass(frozen=True)
class CliBackend:
    command: str
    bin_env: str
    login_hint: str


API_BACKENDS: dict[str, ApiBackend] = {
    "openrouter": ApiBackend(
        "OpenRouter",
        openrouter_key_path,
        "OPENROUTER_API_KEY",
        lambda: f"{openrouter_base()}/key",
    ),
    "openai": ApiBackend(
        "OpenAI",
        openai_key_path,
        "OPENAI_API_KEY",
        lambda: f"{openai_base()}/models",
    ),
    "anthropic": ApiBackend(
        "Anthropic",
        anthropic_key_path,
        "ANTHROPIC_API_KEY",
        lambda: f"{anthropic_base()}/v1/models",
    ),
    "xai": ApiBackend(
        "xAI",
        xai_key_path,
        "XAI_API_KEY",
        lambda: f"{xai_base()}/models",
    ),
    "nous": ApiBackend(
        "Nous Portal",
        nous_key_path,
        "NOUS_API_KEY",
        lambda: f"{nous_base()}/models",
    ),
}

SUBSCRIPTION: dict[str, CliBackend] = {
    "codex": CliBackend("codex", CLI_BIN_ENV["codex"], "`codex login`"),
    "claude-code": CliBackend(
        "claude",
        CLI_BIN_ENV["claude-code"],
        "`claude auth login`",
    ),
    "copilot": CliBackend("copilot", CLI_BIN_ENV["copilot"], "`copilot login` or `/login`"),
    "grok-build": CliBackend(
        "grok",
        CLI_BIN_ENV["grok-build"],
        "`grok login` (or `grok login --device-auth`)",
    ),
    "antigravity": CliBackend(
        "agy",
        CLI_BIN_ENV["antigravity"],
        "an interactive `agy` session",
    ),
}

_SUBSCRIPTION_ORDER = ("codex", "claude-code", "copilot", "grok-build", "antigravity")

_MENU = {
    "openrouter": "OpenRouter API key (default)",
    "openai": "OpenAI API key",
    "anthropic": "Anthropic API key",
    "xai": "xAI API key",
    "nous": "Nous Portal (advanced: needs a Hermes Agent sign-in, see the README)",
    "chatgpt": "ChatGPT plan (sign in once with `codex login`; at your own risk, see the README)",
    "codex": "Codex subscription CLI",
    "claude-code": "Claude Code subscription CLI",
    "copilot": "GitHub Copilot subscription CLI",
    "grok-build": "Grok Build subscription CLI",
    "antigravity": "Google Antigravity subscription CLI",
}

_UNKNOWN = (
    "Unknown backend. Choose openrouter, openai, anthropic, xai, nous, chatgpt, codex, "
    "claude-code, copilot, grok-build, or antigravity."
)


def resolved_cli(backend: str) -> str | None:
    spec = SUBSCRIPTION.get(backend)
    if spec is None:
        return None
    return resolve_binary(spec.bin_env, spec.command)


def discovered_cli_bins() -> dict[str, str]:
    """Absolute paths for subscription CLIs visible at start.

    A missing ``NEXUS_*_BIN`` override is omitted. It does not fall
    through to PATH or the Homebrew prefixes.
    """
    found: dict[str, str] = {}
    for name in _SUBSCRIPTION_ORDER:
        path = resolved_cli(name)
        if path:
            found[SUBSCRIPTION[name].bin_env] = path
    return found


def menu_backends() -> list[tuple[str, str]]:
    """Every backend the interactive menu lists, with a note when it is not usable yet.

    API keys first, then the ChatGPT plan, then the plan CLIs (installed or not, so a new
    user can see they exist), and Nous last because it needs another tool's sign-in.
    """
    rows: list[tuple[str, str]] = [
        (name, _MENU[name]) for name in ("openrouter", "openai", "anthropic", "xai", "chatgpt")
    ]
    for name in _SUBSCRIPTION_ORDER:
        label = _MENU[name]
        if name == "antigravity" and not antigravity_enabled():
            label += f" (opt-in: set {ANTIGRAVITY_ENABLE_ENV}=1 first)"
        elif not resolved_cli(name):
            label += " (not installed)"
        rows.append((name, label))
    rows.append(("nous", _MENU["nous"]))
    return rows


def offered_backends() -> list[str]:
    names = ["openrouter", "openai", "anthropic", "xai", "nous"]
    for name in _SUBSCRIPTION_ORDER:
        if name == "antigravity" and not antigravity_enabled():
            continue
        if resolved_cli(name):
            names.append(name)
    return names


def missing_cli_message(backend: str) -> str:
    spec = SUBSCRIPTION.get(backend)
    if backend == "codex":
        return (
            "codex is not on PATH. Install with `npm i -g @openai/codex` "
            "or the Homebrew cask, then `codex login`, and run setup again."
        )
    if backend == "claude-code":
        return (
            "claude is not on PATH. Install with `brew install --cask claude-code` "
            "or `npm i -g @anthropic-ai/claude-code`, then `claude auth login`. "
            "The dry probe is `claude auth status`. Run setup again."
        )
    if backend == "copilot":
        return (
            "copilot is not on PATH. `gh` is the GitHub CLI, not this backend. "
            "Install with `npm i -g @github/copilot` or `brew install copilot-cli`, "
            "then `copilot login` or `/login`, and run setup again."
        )
    if backend == "antigravity":
        return (
            "agy is not on PATH. Prefer `brew install --cask antigravity-cli` "
            "(binary `agy`). `curl https://antigravity.google/cli/install.sh` "
            "is the other installer. Sign in by running `agy` "
            "(Google account or API key). The deprecated `gemini` CLI is not "
            "this backend. Then run setup again."
        )
    if spec is None:
        return "That CLI is not on PATH."
    return (
        f"{spec.command} is not on PATH. Install the official CLI and sign in "
        f"with {spec.login_hint}, then run setup again."
    )


def key_file_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def provider_note(backend: str) -> str:
    spec = API_BACKENDS.get(backend)
    if spec is None:
        return ""
    path = spec.key_path()
    if key_file_present(path):
        return ""
    return (
        f"{spec.label} key file is absent ({path}). "
        "Chat stays unavailable until ./nexus setup writes it."
    )


def write_private_line(path: Path, line: str, *, mode: int = 0o600) -> None:
    if line == "" or any(char.isspace() for char in line):
        raise SetupError("refusing an empty or multi-token value", code="key_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, mode)
    except OSError as exc:
        raise SetupError(f"Could not write {path}.", code="write_failed") from exc
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    except OSError as exc:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise SetupError(f"Could not write {path}.", code="write_failed") from exc
    os.close(fd)
    try:
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        os.chmod(path, mode)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"Could not write {path}.", code="write_failed") from exc


def connect_steps(
    *,
    backend: str,
    model: str,
    bridge_key: Path,
    provider_key: Path | None,
) -> str:
    lines = [
        "OS3 CONNECT",
        f"  Base URL: http://{DEFAULT_HOST}:{configured_port()}/v1",
        f"  API key file: {bridge_key}",
        "  Header: Authorization: Bearer <the one line in that file>",
        f"  Model: {model}",
        f"  Backend: {backend}",
        "",
        "Paste that base URL into the OS3 harness (OS3 Local / Cipher).",
        "Read the API key file only to paste it into the client.",
        "",
    ]
    if provider_key is not None:
        lines.append(f"Provider key file: {provider_key} (mode 600)")
    elif backend == "codex":
        lines.append("Codex uses the login from `codex login`. This setup leaves that login there.")
    elif backend == "chatgpt":
        lines.append(
            "ChatGPT plan reads the ChatGPT sign-in that `codex login` saved "
            "and refreshes it in place. It is not copied anywhere."
        )
    elif backend == "claude-code":
        lines.append(
            "Claude Code uses the login from `claude auth login`. "
            "The dry probe is `claude auth status`. This setup leaves that login there."
        )
    elif backend in SUBSCRIPTION:
        spec = SUBSCRIPTION[backend]
        lines.append(
            f"{_MENU[backend]} uses the login from {spec.login_hint}. "
            "This setup leaves that login there."
        )
    lines.extend(
        [
            "",
            "The packaged service uses this model.",
            "Set NEXUS_MODEL in the shell before ./nexus start to override it.",
            "",
            "Next:",
            "  ./nexus start",
            "  ./nexus test",
            "  ./doctor.sh",
            "",
            "doctor.sh is the check that the door is working.",
            "/health does not claim tool or image support.",
        ]
    )
    return "\n".join(lines) + "\n"


def _alias_note(raw: str, chosen: str) -> str | None:
    given = raw.strip().lower()
    if given == chosen:
        return None
    if given == "claude" and chosen == "anthropic":
        return "The name claude is the Anthropic API. The subscription CLI is claude-code."
    if given == "grok" and chosen == "xai":
        return "The name grok is the xAI API."
    if given == "grok-cli" and chosen == "grok-build":
        return "The name grok-cli is the Grok Build CLI. The name grok is the xAI API."
    if given == "github-copilot" and chosen == "copilot":
        return "The name github-copilot is the Copilot CLI."
    if given == "agy" and chosen == "antigravity":
        return "The name agy is the Antigravity CLI."
    return None


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise SetupError(f"Could not set mode 600 on {path}.", code="write_failed") from exc
    if private_mode(path) != "600":
        raise SetupError(f"Could not set mode 600 on {path}.", code="write_failed")


_MIN_KEY_LENGTH = 20

_PLACEHOLDER_MARKERS = (
    "sk-or-v1-dum",
    "dummy",
    "placeholder",
    "your-api-key",
    "your_api_key",
    "paste-your",
    "insert-your",
    "changeme",
    "change-me",
    "replace-me",
    "not-a-real",
    "example-key",
    "redacted",
    "xxxxx",
)


def placeholder_reason(key: str) -> str | None:
    """Why this value cannot be a live credential, or None when it can be.

    Local only — no network. A placeholder sat in the shipped key file and
    nothing noticed until the first chat returned an upstream 401, so the
    obvious shapes are rejected before anything trusts them.
    """
    if len(key) < _MIN_KEY_LENGTH:
        return "is too short to be a real key"
    lowered = key.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            return "looks like a placeholder, not a real key"
    return None


@dataclass(frozen=True)
class VerifyResult:
    """One authenticated call's verdict on a candidate key.

    ``accepted`` is a tri-state: True the provider accepted the key, False it
    answered and refused it (401/403), None the check proved nothing —
    unreachable or an answer that is not an auth verdict. ``note`` is
    printable and never carries key material.
    """

    accepted: bool | None
    note: str


_VERIFY_TIMEOUT = 10.0


def _verify_headers(backend: str, key: str) -> dict[str, str]:
    if backend == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    return {"Authorization": f"Bearer {key}"}


def verify_provider_key(backend: str, key: str) -> VerifyResult:
    """One cheap authenticated GET to prove a key is live before setup trusts it.

    Writing whatever was typed and reporting success is how the default path
    shipped a dead credential. Only 401/403 counts as a rejection; any other
    answer leaves the verdict inconclusive rather than guessed.
    """
    spec = API_BACKENDS.get(backend)
    if spec is None:
        return VerifyResult(True, "not an API-key backend; nothing to check")
    try:
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(_VERIFY_TIMEOUT),
        ) as client:
            response = client.get(spec.verify_url(), headers=_verify_headers(backend, key))
    except httpx.HTTPError as exc:
        return VerifyResult(
            None, f"could not reach {spec.label} to check the key ({type(exc).__name__})"
        )
    status = response.status_code
    if 200 <= status < 300:
        return VerifyResult(True, f"{spec.label} accepted the key (HTTP {status})")
    if status in {401, 403}:
        return VerifyResult(False, f"{spec.label} rejected the key (HTTP {status})")
    return VerifyResult(None, f"{spec.label} answered HTTP {status} to the key check")


def _check_candidate_key(spec: ApiBackend, *, backend: str, key: str) -> None:
    """Reject a key that cannot work before anything trusts it.

    Placeholders are caught locally; everything else must survive one
    authenticated call upstream. Raises SetupError with a printable reason —
    setup does not get to report success on an unverified credential.
    """
    reason = placeholder_reason(key)
    if reason is not None:
        raise SetupError(f"The {spec.label} API key {reason}.", code="key_invalid")
    verdict = verify_provider_key(backend, key)
    if verdict.accepted is True:
        return
    note = redact(verdict.note, [key])
    if verdict.accepted is False:
        raise SetupError(
            f"{note}. The key is wrong or expired; fix it and run setup again.", code="key_rejected"
        )
    raise SetupError(
        f"{note}. The {spec.label} key could not be verified; "
        "check connectivity and run setup again.",
        code="key_unverified",
    )


def _clean_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise SetupError(
            "An API key is required. Paste the key, then press Enter.", code="key_invalid"
        )
    if any(char.isspace() for char in key):
        raise SetupError("The API key must be one line with no spaces.", code="key_invalid")
    if len(key) > 4096:
        raise SetupError("The API key is too long.", code="key_invalid")
    return key


def _read_line(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _guard(text: str, *secrets: str | None) -> None:
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in text:
            raise SetupError("refusing to print a key")


def _require_antigravity_opt_in(backend: str) -> None:
    if backend == "antigravity" and not antigravity_enabled():
        raise SetupError(
            "Antigravity is off by default: it carries higher terms-of-service risk. "
            f"Set {ANTIGRAVITY_ENABLE_ENV}=1 to opt in, then run setup again. "
            "See 'Provider risk' in the README."
        )


def _prompt_backend(ask: Callable[[str], str]) -> str:
    rows = menu_backends()
    print("Backend:")
    for index, (_name, label) in enumerate(rows, start=1):
        print(f"  {index}. {label}")
    try:
        raw = ask("Choice [1]: ")
    except EOFError as exc:
        raise SetupError("Pass --backend. The default is openrouter.") from exc
    raw = raw.strip()
    if raw == "":
        return "openrouter"
    if raw.isdigit():
        index = int(raw)
        if not 1 <= index <= len(rows):
            raise SetupError("Choose a number from the list.")
        chosen = rows[index - 1][0]
    else:
        chosen = canonical_backend(raw)
    if chosen not in BACKEND_IDS:
        raise SetupError(_UNKNOWN)
    _require_antigravity_opt_in(chosen)
    if chosen in SUBSCRIPTION and chosen not in offered_backends():
        raise SetupError(missing_cli_message(chosen))
    return chosen


def _store_api_key(
    spec: ApiBackend,
    *,
    backend: str,
    api_key: str | None,
    non_interactive: bool,
    ask: Callable[[str], str],
    secret_fn: Callable[[str], str],
) -> tuple[str, str | None]:
    path = spec.key_path()
    if api_key is None and key_file_present(path):
        replace = False
        if not non_interactive:
            try:
                answer = ask(f"A {spec.label} key file already exists. Replace it? [y/N]: ")
            except EOFError as exc:
                raise SetupError("Pass --api-key-stdin to replace the key file.") from exc
            replace = answer.strip().lower() in {"y", "yes"}
        if not replace:
            _chmod_private(path)
            kept = _read_line(path)
            if kept is not None:
                _check_candidate_key(spec, backend=backend, key=kept)
            return f"Keeping the existing {spec.label} key file ({path}, mode 600).", kept
        try:
            api_key = secret_fn(f"{spec.label} API key: ")
        except EOFError as exc:
            raise SetupError("Pass --api-key-stdin to write the key file.") from exc
    if api_key is None:
        if non_interactive:
            raise SetupError(
                f"Pass the {spec.label} API key on stdin: "
                f"./nexus setup --backend {backend} --api-key-stdin"
            )
        try:
            api_key = secret_fn(f"{spec.label} API key: ")
        except EOFError as exc:
            raise SetupError("Pass --api-key-stdin to write the key file.") from exc
    key = _clean_key(api_key)
    _check_candidate_key(spec, backend=backend, key=key)
    target = spec.key_write_path()
    write_private_line(target, key)
    if private_mode(target) != "600":
        raise SetupError(f"Could not set mode 600 on {target}.", code="write_failed")
    return f"Wrote the {spec.label} key file ({target}, mode 600).", key


def clear_model_choice() -> None:
    path = model_choice_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise SetupError(f"Could not remove {path}.", code="write_failed") from exc


def _clean_model(value: str) -> str:
    model = value.strip()
    if not valid_model_id(model):
        raise SetupError(
            "Model must be one id made of letters, numbers, and . _ : / -", code="invalid_request"
        )
    return model


def _store_model(model: str | None) -> str:
    """Write the model file, or remove it when the field is blank."""
    if model is None or not model.strip():
        clear_model_choice()
        return "Using this backend's built-in default."
    cleaned = _clean_model(model)
    write_private_line(model_choice_path(), cleaned, mode=0o644)
    note = "Model saved. The next request uses it when the client does not name one."
    if (environ_get("NEXUS_MODEL") or "").strip():
        note += " NEXUS_MODEL is set in this process and still wins until restart."
    return note


def _require_ready(chosen: str) -> dict[str, Any]:
    """The /health row for ``chosen``. Refuses to activate one that is not ready.

    ``base_state`` is the same check /health and Settings use for CLI login and
    a key on file. ``describe()`` is where that row comes from.
    """
    row = build_backend(chosen).describe()
    if base_state(row) == READY:
        return row
    reason = str(row.get("hint") or "This backend is not ready.")
    raise SetupError(
        reason,
        code="not_ready",
        hint="Nothing was changed. Fix this, then Save again.",
    )


def _model_ids(row: dict[str, Any]) -> set[str]:
    ids = {item for item in row.get("models") or [] if isinstance(item, str) and item}
    default = row.get("default_model")
    if isinstance(default, str) and default:
        ids.add(default)
    return ids


def _default_model_name(chosen: str, row: dict[str, Any]) -> str:
    default = row.get("default_model")
    if isinstance(default, str) and default:
        return default
    return BACKEND_DEFAULT_MODELS.get(chosen, "")


def _model_on_switch(
    chosen: str, previous: str | None, model: str | None, row: dict[str, Any]
) -> str:
    """Keep a model only when this backend can serve it. Otherwise use its default.

    A save that does not change the backend keeps today's rule: a blank model
    clears the file. A switch never copies another backend's id across.
    """
    if previous == chosen:
        return _store_model(model)
    ids = _model_ids(row)
    default = _default_model_name(chosen, row)
    saved = model_choice_from_file()
    posted = model.strip() if isinstance(model, str) and model.strip() else None
    if posted:
        posted = _clean_model(posted)
        if posted != saved or posted in ids:
            return _store_model(posted)
        clear_model_choice()
        return _env_model_note(
            f"Reset the model to {default}. {posted} is not a model this backend serves."
        )
    if saved and saved in ids:
        return _env_model_note(f"Kept {saved}. It is a model this backend serves.")
    clear_model_choice()
    if saved:
        return _env_model_note(
            f"Reset the model to {default}. {saved} is not a model this backend serves."
        )
    return _env_model_note(f"Using {default}, this backend's built-in default.")


def _env_model_note(note: str) -> str:
    if (environ_get("NEXUS_MODEL") or "").strip():
        return note + " NEXUS_MODEL is set in this process and still wins until restart."
    return note


def apply_dashboard_choice(
    *,
    backend: str,
    api_key: str | None,
    model: str | None,
    activate: bool = True,
) -> dict[str, str]:
    """Write the setup choice file, and a key only when one was typed.

    ``activate=False`` saves a typed key for an API backend and nothing else: the
    active backend and the saved model are left alone, and ``model`` is ignored.

    Returns paths and presence flags. Never a key, and never a printed setup
    note — those notes can include the bridge key.
    """
    chosen = canonical_backend(backend.strip())
    if chosen not in BACKEND_IDS:
        raise SetupError(_UNKNOWN, code="unknown_backend")
    if not activate:
        return _save_key_only(chosen, api_key)
    if model is not None and model.strip():
        _clean_model(model)
    wrote_key = False
    key_path: Path | None = None
    if chosen in API_BACKENDS:
        spec = API_BACKENDS[chosen]
        key_path = spec.key_path()
        if api_key is not None and api_key.strip():
            cleaned = _clean_key(api_key)
            _check_candidate_key(spec, backend=chosen, key=cleaned)
            target = spec.key_write_path()
            write_private_line(target, cleaned)
            if private_mode(target) != "600":
                raise SetupError(f"Could not set mode 600 on {target}.", code="write_failed")
            wrote_key = True
        elif not key_file_present(key_path):
            raise SetupError(
                f"No {spec.label} key file yet. Type a key, or run "
                f"./nexus setup --backend {chosen} --api-key-stdin",
                code="key_missing",
            )
    else:
        if api_key is not None and api_key.strip():
            raise SetupError(
                f"{chosen} uses the CLI login. Leave the key field empty.", code="cli_has_no_key"
            )
        if chosen != "chatgpt" and resolved_cli(chosen) is None:
            raise SetupError(missing_cli_message(chosen), code="cli_missing")
    previous = backend_choice_from_file()
    row = _require_ready(chosen)
    write_private_line(backend_choice_path(), chosen, mode=0o644)
    ensure_bridge_key()
    model_note = _model_on_switch(chosen, previous, model, row)
    advice = dashboard_restart_advice()
    return {
        "backend": chosen,
        "choice_file": str(backend_choice_path()),
        "model": model_choice_from_file() or "",
        "model_file": str(model_choice_path()),
        "key_written": "yes" if wrote_key else "no",
        "key_file": str(key_path) if key_path is not None else "",
        "model_note": model_note,
        "restart": (
            "The door is serving this backend now. Requests already in flight "
            "finish on the previous one."
        ),
        "restart_available": "no",
        "restart_command": "",
        "restart_managed": advice["managed"],
        "activated": "yes",
    }


def _save_key_only(chosen: str, api_key: str | None) -> dict[str, str]:
    """The ``activate: false`` half of ``apply_dashboard_choice``: one verified key, no switch."""
    spec = API_BACKENDS.get(chosen)
    if spec is None:
        raise SetupError(
            f"{chosen} uses the CLI login; there is no key to save.", code="cli_has_no_key"
        )
    if api_key is None or not api_key.strip():
        raise SetupError(f"Type a {spec.label} key to save.", code="key_missing")
    key_path = spec.key_write_path()
    cleaned = _clean_key(api_key)
    _check_candidate_key(spec, backend=chosen, key=cleaned)
    write_private_line(key_path, cleaned)
    if private_mode(key_path) != "600":
        raise SetupError(f"Could not set mode 600 on {key_path}.", code="write_failed")
    advice = dashboard_restart_advice()
    return {
        "backend": active_backend_id(),
        "choice_file": str(backend_choice_path()),
        "model": model_choice_from_file() or "",
        "model_file": str(model_choice_path()),
        "key_written": "yes",
        "key_file": str(key_path),
        "model_note": "Backend and model unchanged.",
        "restart": advice["note"],
        "restart_available": advice["available"],
        "restart_command": advice["command"],
        "restart_managed": advice["managed"],
        "activated": "no",
    }


def run_setup(
    *,
    backend: str | None,
    api_key: str | None,
    non_interactive: bool,
    input_fn: Callable[[str], str] | None = None,
    secret_fn: Callable[[str], str] | None = None,
) -> str:
    ask = input_fn or input
    read_secret = secret_fn or getpass.getpass
    raw = backend
    if raw is None:
        if non_interactive:
            raise SetupError(
                "Pass --backend. The default is openrouter: --backend openrouter --api-key-stdin"
            )
        raw = _prompt_backend(ask)
    chosen = canonical_backend(raw)
    if chosen not in BACKEND_IDS:
        raise SetupError(_UNKNOWN)
    notes: list[str] = []
    alias = _alias_note(raw, chosen)
    if alias:
        notes.append(alias)
    provider_path: Path | None = None
    stored: str | None = None
    if chosen in API_BACKENDS:
        spec = API_BACKENDS[chosen]
        provider_path = spec.key_path()
        note, stored = _store_api_key(
            spec,
            backend=chosen,
            api_key=api_key,
            non_interactive=non_interactive,
            ask=ask,
            secret_fn=read_secret,
        )
        notes.append(note)
    else:
        if api_key is not None and api_key.strip():
            raise SetupError(f"{chosen} uses the CLI login. Run setup without an API key.")
        _require_antigravity_opt_in(chosen)
        binary = resolved_cli(chosen) if chosen != "chatgpt" else "chatgpt"
        if chosen == "chatgpt":
            status, hint = chatgpt_login_status()
            if status != "ok":
                raise SetupError(hint)
            notes.append("ChatGPT plan: uses the `codex login` ChatGPT sign-in, refreshed in place")
        elif binary is None:
            raise SetupError(missing_cli_message(chosen))
        elif chosen == "codex":
            notes.append(f"Codex CLI: {binary}. Sign-in stays in `codex login`.")
        elif chosen == "claude-code":
            notes.append(f"Claude Code CLI: {binary}. Sign-in stays in the claude CLI (/login).")
        else:
            spec = SUBSCRIPTION[chosen]
            notes.append(f"{_MENU[chosen]}: {binary}. Sign-in stays in {spec.login_hint}.")
    if chosen not in BACKEND_IDS:
        raise SetupError("Unknown backend.")
    previous = backend_choice_from_file()
    write_private_line(backend_choice_path(), chosen, mode=0o644)
    if previous != chosen:
        clear_model_choice()
    saved_model = model_choice_from_file() or BACKEND_DEFAULT_MODELS[chosen]
    bridge = ensure_bridge_key()
    bridge_path = bridge_key_path()
    _chmod_private(bridge_path)
    text = (
        "\n".join(notes)
        + "\n\n"
        + connect_steps(
            backend=chosen,
            model=saved_model,
            bridge_key=bridge_path,
            provider_key=provider_path,
        )
    )
    _guard(text, api_key, stored, bridge, _read_line(bridge_path))
    return text


def provider_credential_report() -> tuple[int, list[str]]:
    """doctor.sh's credential verdict: an exit code and printable lines.

    Subscription CLIs hold no key file, so there is nothing to check. An
    absent key is the readiness probe's job and does not fail here. Only a
    configured credential is judged: a placeholder fails outright, an upstream
    refusal fails, and an unanswered check is a warning, not a verdict.
    """
    backend = active_backend_id()
    spec = API_BACKENDS.get(backend)
    if spec is None:
        if backend in SUBSCRIPTION or backend == "chatgpt":
            return 0, [f"credential: SKIP {backend} signs in with its CLI, not a key file"]
        return 0, [f"credential: SKIP backend '{backend}' has no key check"]
    key = load_key(env_var=spec.env_var, path=spec.key_path())
    if key is None:
        return 0, [
            f"credential: SKIP no {spec.label} key configured; the status check reports readiness"
        ]
    replace = f"Replace it: ./nexus setup --backend {backend} --api-key-stdin"
    reason = placeholder_reason(key)
    if reason is not None:
        return 2, [f"credential: FAIL the configured {spec.label} key {reason}. {replace}"]
    verdict = verify_provider_key(backend, key)
    note = redact(verdict.note, [key])
    if verdict.accepted is True:
        return 0, [f"credential: PASS {note}"]
    if verdict.accepted is False:
        return 2, [f"credential: FAIL {note}. {replace}"]
    return 0, [f"credential: WARN {note}; the key could not be confirmed"]
