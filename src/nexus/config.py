"""Environment and paths. Read on each call so key rotation does not need a restart."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def canonical_env(name: str) -> str | None:
    """The ``NEXUS_*`` spelling of a bridge setting, or None for an unrelated variable."""
    if name.startswith("NEXUS_"):
        return name
    if name.startswith("OS3_BRIDGE_"):
        return "NEXUS_" + name[len("OS3_BRIDGE_") :]
    if name == "OS3_UI_DIST":
        return "NEXUS_UI_DIST"
    return None


def environ_get(name: str, default: str | None = None) -> str | None:
    """Read a setting. A set ``NEXUS_*`` wins, even when empty. Else the legacy name.

    ``name`` may be either spelling. ``OS3_UI_DIST`` is the historical name for
    ``NEXUS_UI_DIST``; ``OS3_BRIDGE_UI_DIST`` is accepted too. Anything else is
    read as itself.
    """
    primary = canonical_env(name)
    if primary is None:
        return os.environ[name] if name in os.environ else default
    if primary in os.environ:
        return os.environ[primary]
    if primary == "NEXUS_UI_DIST":
        for legacy in ("OS3_BRIDGE_UI_DIST", "OS3_UI_DIST"):
            if legacy in os.environ:
                return os.environ[legacy]
        return default
    legacy = "OS3_BRIDGE_" + primary[len("NEXUS_") :]
    if legacy in os.environ:
        return os.environ[legacy]
    return default


def setting_name(suffix: str) -> str:
    """Which spelling is set, so an error names the variable the operator actually set."""
    primary = "NEXUS_" + suffix
    if primary in os.environ:
        return primary
    legacy = "OS3_BRIDGE_" + suffix
    if legacy in os.environ:
        return legacy
    return primary


def export_setting(env: dict[str, str], name: str, value: str) -> None:
    """Write ``NEXUS_*`` and the ``OS3_BRIDGE_*`` alias an installed unit already uses."""
    primary = canonical_env(name)
    if primary is None:
        env[name] = value
        return
    env[primary] = value
    if primary == "NEXUS_UI_DIST":
        env["OS3_UI_DIST"] = value
        return
    env["OS3_BRIDGE_" + primary[len("NEXUS_") :]] = value


def state_path(env_suffix: str, filename: str, legacy_filename: str) -> Path:
    """Choice-file path. An env override wins. Else the new name, else the legacy file.

    The legacy file is used only when it exists and the new file does not, so an
    install that already has ``.os3_bridge_backend`` keeps that one file.
    """
    raw = (environ_get("NEXUS_" + env_suffix) or "").strip()
    if raw:
        return Path(raw).expanduser()
    root = repo_root()
    current = root / filename
    legacy = root / legacy_filename
    try:
        if current.exists() or not legacy.exists():
            return current
    except OSError:
        return current
    return legacy


DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11435
_LOOPBACK = {"127.0.0.1", "localhost"}

BACKEND_ALIASES = {
    "claude": "anthropic",
    "grok": "xai",
    "claude-cli": "claude-code",
    "github-copilot": "copilot",
    "grok-cli": "grok-build",
    "agy": "antigravity",
    "chatgpt-plan": "chatgpt",
}
BACKEND_DEFAULT_MODELS = {
    "openrouter": DEFAULT_MODEL,
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "xai": "grok-4",
    "nous": "meituan/longcat-2.5-preview:free",
    "codex": "gpt-5.6-sol",
    "chatgpt": "gpt-6-luna",
    "claude-code": "claude-sonnet-4-5",
    "copilot": "claude-sonnet-4.6",
    "grok-build": "grok-4.7",
    "antigravity": "gemini-3.8-flash-high",
}
CLI_BIN_ENV = {
    "codex": "NEXUS_CODEX_BIN",
    "claude-code": "NEXUS_CLAUDE_BIN",
    "copilot": "NEXUS_COPILOT_BIN",
    "grok-build": "NEXUS_GROK_BIN",
    "antigravity": "NEXUS_AGY_BIN",
}
BACKEND_IDS = frozenset(BACKEND_DEFAULT_MODELS)
ANTIGRAVITY_ENABLE_ENV = "NEXUS_ENABLE_ANTIGRAVITY"


def antigravity_enabled() -> bool:
    """Antigravity is the one subscription backend that is off until the user opts in."""
    raw = environ_get(ANTIGRAVITY_ENABLE_ENV) or ""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def bridge_key_path() -> Path:
    return state_path("KEY_FILE", ".nexus_api_key", ".os3_bridge_api_key")


def canonical_backend(name: str) -> str:
    raw = name.strip().lower()
    return BACKEND_ALIASES.get(raw, raw)


def backend_choice_path() -> Path:
    """Backend name recorded by setup. Not a secret. The saved file wins over the backend env."""
    return state_path("BACKEND_FILE", ".nexus_backend", ".os3_bridge_backend")


def backend_choice_from_file() -> str | None:
    path = backend_choice_path()
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if not raw or any(char.isspace() for char in raw):
        return None
    return canonical_backend(raw)


def active_backend_id() -> str:
    """Process backend. Saved setup choice, then the backend env, then openrouter.

    The choice file is what Settings writes. A packaged unit still sets
    ``OS3_BRIDGE_BACKEND`` (the LaunchAgent default); ``NEXUS_BACKEND`` wins
    when both are set. The env is only used when nothing has been saved yet.
    Read on every call so a Settings save is the next request's backend
    without a restart.
    """
    saved = backend_choice_from_file()
    if saved:
        return saved
    raw = (environ_get("NEXUS_BACKEND") or "").strip().lower()
    if raw:
        return canonical_backend(raw)
    return "openrouter"


def service_backend() -> str:
    """Backend ``./nexus start`` bakes into the packaged service."""
    return backend_choice_from_file() or "openrouter"


def valid_model_id(value: str) -> bool:
    """One model id. Slashes and colons are allowed; whitespace is not."""
    if not value or len(value) > 128 or any(char.isspace() for char in value):
        return False
    if not value[0].isalnum():
        return False
    return all(char.isalnum() or char in "._:/-" for char in value)


def model_choice_path() -> Path:
    """Model id saved from the dashboard for the backend choice file. Not a secret."""
    return state_path("MODEL_FILE", ".nexus_model", ".os3_bridge_model")


def model_choice_from_file() -> str | None:
    path = model_choice_path()
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not valid_model_id(raw):
        return None
    return raw


def backend_model(backend_id: str) -> str:
    """This backend's own model: the one saved for it, else its starting default.

    Ignores ``NEXUS_MODEL``, which names the active backend's model and means nothing
    to any other backend. The fallback chain uses this for every backend but the first.
    """
    canonical = canonical_backend(backend_id)
    saved = model_choice_from_file()
    if saved and backend_choice_from_file() == canonical:
        return saved
    return BACKEND_DEFAULT_MODELS.get(canonical, DEFAULT_MODEL)


def model_for(backend_id: str) -> str:
    """``NEXUS_MODEL`` wins (legacy ``OS3_BRIDGE_MODEL`` when that is unset).

    Then the saved model for this backend. Then its default. The saved id applies
    only to the backend named in the choice file, so picking a model for OpenRouter
    does not rewrite Anthropic's default.
    """
    override = (environ_get("NEXUS_MODEL") or "").strip()
    if override:
        return override
    return backend_model(backend_id)


def default_model() -> str:
    return model_for(active_backend_id())


def backend_source() -> str:
    """Where ``active_backend_id()`` got its answer: ``file``, ``env``, or ``default``."""
    if backend_choice_from_file():
        return "file"
    if (environ_get("NEXUS_BACKEND") or "").strip():
        return "env"
    return "default"


def model_source() -> str:
    """Where ``default_model()`` got its answer, mirroring the precedence in ``model_for``."""
    if (environ_get("NEXUS_MODEL") or "").strip():
        return "env"
    saved = model_choice_from_file()
    if saved and backend_choice_from_file() == canonical_backend(active_backend_id()):
        return "file"
    return "default"


def ui_dist_dir() -> Path:
    """Where a built Nexus UI lives. ``NEXUS_UI_DIST`` overrides ``<repo>/ui/dist``."""
    raw = (environ_get("NEXUS_UI_DIST") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return repo_root() / "ui" / "dist"


PROFILE_DIRNAME = "os3-chrome-debug"
_warned_both: set[tuple[str, str]] = set()
_log = logging.getLogger("nexus.config")


def _is_mac() -> bool:
    return sys.platform == "darwin"


def _config_base() -> Path:
    if _is_mac():
        return Path.home() / "Library/Application Support"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg).expanduser() if xdg else Path.home() / ".config"


def data_dir() -> Path:
    """Where Nexus keeps provider key files and the Rabbit Chrome profile."""
    return _config_base() / ("Nexus" if _is_mac() else "nexus")


def legacy_key_dir() -> Path:
    """Pre-Nexus key folder: GrokBot/os3-setup on macOS, ``os3-bridge`` elsewhere."""
    return _config_base() / ("GrokBot/os3-setup" if _is_mac() else "os3-bridge")


def legacy_profile_dir() -> Path:
    base = _config_base()
    if _is_mac():
        return base / "GrokBot" / PROFILE_DIRNAME
    return base / "os3-bridge" / PROFILE_DIRNAME


def prefer_new_path(new: Path, legacy: Path) -> Path:
    """The new path, unless only the legacy one exists. Both existing: new wins, one warning."""
    try:
        legacy_there = legacy.exists()
        new_there = new.exists()
    except OSError:
        return new
    if not legacy_there:
        return new
    if not new_there:
        return legacy
    if (str(new), str(legacy)) not in _warned_both:
        _warned_both.add((str(new), str(legacy)))
        _log.warning(
            "both %s and the legacy %s exist; using %s. Nothing was moved or deleted. "
            "Move the old files by hand when ready (docs/upgrading-from-os3-bridge.md).",
            new,
            legacy,
            new,
        )
    return new


def _legacy_keys_only() -> bool:
    """A legacy key folder and no Nexus folder: every key file stays in the legacy one."""
    try:
        return legacy_key_dir().is_dir() and not data_dir().exists()
    except OSError:
        return False


def _platform_key(mac_filename: str, xdg_filename: str) -> Path:
    """Read path. Legacy folder when it is the only one; else Nexus, falling back per file."""
    name = mac_filename if _is_mac() else xdg_filename
    if _legacy_keys_only():
        return legacy_key_dir() / name
    return prefer_new_path(data_dir() / name, legacy_key_dir() / name)


def key_write_path(path: Path, override_env: str) -> Path:
    """Where to save a key file whose read path is ``path``.

    A path set through ``override_env`` (``NEXUS_*_KEY_FILE``) is always written as given.
    Otherwise a legacy-folder file is rewritten in place only while the Nexus folder does not
    exist. Once it does, the write goes there, so a read fallback never makes a second legacy file.
    """
    if (environ_get(override_env) or "").strip():
        return path
    if path.parent == legacy_key_dir() and data_dir().exists():
        return data_dir() / path.name
    return path


def _key_path(env_name: str, mac_filename: str, xdg_filename: str) -> Path:
    raw = (environ_get(env_name) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _platform_key(mac_filename, xdg_filename)


def default_openrouter_key_file() -> Path:
    return _platform_key(".openrouter-os3.key", "openrouter.key")


def openrouter_key_path() -> Path:
    return _key_path(
        "NEXUS_OPENROUTER_KEY_FILE",
        ".openrouter-os3.key",
        "openrouter.key",
    )


def openai_key_path() -> Path:
    return _key_path("NEXUS_OPENAI_KEY_FILE", ".openai-os3.key", "openai.key")


def anthropic_key_path() -> Path:
    return _key_path("NEXUS_ANTHROPIC_KEY_FILE", ".anthropic-os3.key", "anthropic.key")


def xai_key_path() -> Path:
    return _key_path("NEXUS_XAI_KEY_FILE", ".xai-os3.key", "xai.key")


def _base(env_name: str, default: str) -> str:
    raw = (environ_get(env_name, default) or "").strip()
    return (raw or default).rstrip("/")


def openrouter_base() -> str:
    return _base("NEXUS_OPENROUTER_BASE", "https://openrouter.ai/api/v1")


def openai_base() -> str:
    return _base("NEXUS_OPENAI_BASE", "https://api.openai.com/v1")


def anthropic_base() -> str:
    return _base("NEXUS_ANTHROPIC_BASE", "https://api.anthropic.com")


def xai_base() -> str:
    return _base("NEXUS_XAI_BASE", "https://api.x.ai/v1")


def nous_base() -> str:
    return _base("NEXUS_NOUS_BASE", "https://inference-api.nousresearch.com/v1")


def nous_key_path() -> Path:
    return _key_path("NEXUS_NOUS_KEY_FILE", ".nous-os3.key", "nous.key")


def configured_host() -> str:
    return (environ_get("NEXUS_HOST", DEFAULT_HOST) or "").strip() or DEFAULT_HOST


def loopback_host() -> str:
    """Supported listen address. localhost is accepted and normalized."""
    host = configured_host()
    if host not in _LOOPBACK:
        raise SystemExit(
            f"Nexus only binds 127.0.0.1 (got {host}). Refusing a non-loopback listen."
        )
    return DEFAULT_HOST


def configured_port() -> int:
    raw = (environ_get("NEXUS_PORT", str(DEFAULT_PORT)) or "").strip()
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{setting_name('PORT')} is not an integer: {raw}") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"{setting_name('PORT')} out of range: {port}")
    return port


def door_origin() -> str:
    return f"http://{DEFAULT_HOST}:{configured_port()}"


def max_inflight() -> int:
    """In-process cap on concurrent upstream chat calls. Not a per-client quota."""
    raw = (environ_get("NEXUS_MAX_INFLIGHT", "8") or "").strip()
    try:
        limit = int(raw)
    except ValueError:
        limit = 8
    return max(1, min(limit, 64))


def roles_file_path() -> Path:
    """Per-role model ids, as JSON. Not a secret, so it is safe beside the other choice files."""
    return state_path("ROLES_FILE", ".nexus_roles.json", ".os3_bridge_roles.json")


def fallback_file_path() -> Path:
    """Failover chain as JSON, beside the other choice files. Not a secret."""
    return state_path("FALLBACK_FILE", ".nexus_fallback.json", ".os3_bridge_fallback.json")


def tool_repair_enabled() -> bool:
    """On unless NEXUS_TOOL_REPAIR is off/0/false/no. The off switch restores pass-through."""
    raw = (environ_get("NEXUS_TOOL_REPAIR") or "").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def model_fallback_enabled() -> bool:
    """On unless NEXUS_MODEL_FALLBACK is off/0/false/no. Off restores pass-through."""
    raw = (environ_get("NEXUS_MODEL_FALLBACK") or "").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def upstream_timeout() -> float:
    """Shared upstream timeout. The older OpenRouter env still applies when unset."""
    raw = (environ_get("NEXUS_UPSTREAM_TIMEOUT") or "").strip()
    if not raw:
        raw = (environ_get("NEXUS_OPENROUTER_TIMEOUT", "120") or "").strip()
    try:
        timeout = float(raw or "120")
    except ValueError:
        timeout = 120.0
    return max(1.0, timeout)


def openrouter_timeout() -> float:
    return upstream_timeout()


def health_timeout() -> float:
    """Budget for the /health readiness check on a cold cache.

    Deliberately independent of ``upstream_timeout()``, which is the chat
    path's budget. A watchdog polling /health must never inherit the chat
    path's minutes-long allowance, so this defaults to a couple of seconds:
    on a cold cache the check gives up and reports ``model_ready: null``
    rather than blocking the poll.
    """
    raw = (environ_get("NEXUS_HEALTH_TIMEOUT") or "").strip()
    try:
        timeout = float(raw) if raw else 2.0
    except ValueError:
        timeout = 2.0
    return max(0.1, min(timeout, 60.0))


def private_mode(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        return format(path.stat().st_mode & 0o777, "o")
    except OSError:
        return None


def rabbit_watchdog_enabled() -> bool:
    """Off unless NEXUS_RABBIT_WATCHDOG is on/1/true/yes. Off never runs launchctl."""
    raw = (environ_get("NEXUS_RABBIT_WATCHDOG") or "").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def rabbit_agent_label() -> str:
    """launchd label of Rabbit's agent. Override with NEXUS_RABBIT_AGENT_LABEL."""
    return (environ_get("NEXUS_RABBIT_AGENT_LABEL") or "").strip() or "com.rabbit.rabbit-agent"


def rabbit_watchdog_state_path() -> Path:
    """Where kickstart times are kept so the hourly cap survives a restart of this process."""
    return state_path(
        "RABBIT_WATCHDOG_STATE",
        ".nexus_rabbit_watchdog.json",
        ".os3_bridge_rabbit_watchdog.json",
    )


RABBIT_SETUP_ENV = "NEXUS_RABBIT_SETUP"
DEFAULT_RABBIT_CDP_PORT = 9333


def rabbit_setup_enabled() -> bool:
    """Off unless NEXUS_RABBIT_SETUP is on/1/true/yes. Off refuses every Rabbit write."""
    raw = (environ_get(RABBIT_SETUP_ENV) or "").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def rabbit_profile_dir() -> Path:
    """The dedicated Chrome profile the user signed into OS3 once."""
    raw = (environ_get("NEXUS_RABBIT_PROFILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    if _legacy_keys_only():
        return legacy_profile_dir()
    return prefer_new_path(data_dir() / PROFILE_DIRNAME, legacy_profile_dir())


def rabbit_cdp_port() -> int:
    """Chrome remote-debugging port on 127.0.0.1. Raises ValueError for a bad value."""
    raw = (environ_get("NEXUS_RABBIT_CDP_PORT") or "").strip()
    if not raw:
        return DEFAULT_RABBIT_CDP_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{setting_name('RABBIT_CDP_PORT')} is not an integer: {raw}") from None
    if not 1024 <= port <= 65535:
        raise ValueError(f"{setting_name('RABBIT_CDP_PORT')} out of range (1024-65535): {port}")
    return port


def rabbit_chrome_bin() -> str | None:
    """Explicit Chrome binary. Unset: the usual install locations are tried."""
    return (environ_get("NEXUS_RABBIT_CHROME_BIN") or "").strip() or None


def rabbit_backup_path() -> Path:
    """Last OS3 provider config before a setup. Holds a masked key only, mode 600."""
    return state_path(
        "RABBIT_BACKUP",
        ".nexus_rabbit_backup.json",
        ".os3_bridge_rabbit_backup.json",
    )


def rabbit_restore_key_file() -> Path | None:
    """Key file for putting a non-Nexus provider back. Unset: such a provider cannot be restored."""
    raw = (environ_get("NEXUS_RABBIT_RESTORE_KEY_FILE") or "").strip()
    return Path(raw).expanduser() if raw else None


def rabbit_key_dirs() -> list[Path]:
    """Where a restore key file is looked for when NEXUS_RABBIT_RESTORE_KEY_FILE is unset.

    NEXUS_RABBIT_KEY_DIRS (os.pathsep-separated) first, then the ``rabbit-keys/``
    subfolder of the directory that holds the bridge key. That directory itself is not
    searched: in a git checkout it is the repo root. Each directory is searched on its own,
    not recursively. ``OS3_BRIDGE_RABBIT_KEY_DIRS`` still applies when the new name is unset.
    """
    dirs: list[Path] = []
    raw = environ_get("NEXUS_RABBIT_KEY_DIRS") or ""
    dirs += [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    dirs.append(bridge_key_path().parent / "rabbit-keys")
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in dirs:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def rabbit_connection_path() -> Path:
    """Where rabbit-agent records this machine's node id."""
    return Path.home() / ".rabbit-agent" / "connection.json"
