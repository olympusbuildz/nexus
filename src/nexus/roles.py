"""Which OS3 role a chat request comes from, and which model that role should use.

OS3's local mode has one model setting, but it sends three kinds of traffic:

    chat        the main assistant (OS3's "Small"): create_task, steer_task, ...
    worker      the agent that runs tools on a device ("Standard"): shell, files,
                computer_use, with a "You are a worker agent" system prompt
    background  housekeeping (memory and fact extraction, reply review): no tools,
                or a tool that only returns structured data such as emit_facts

and one check that is not a role: the image-understanding probe, a request that
carries an image and no tools. It is classified as ``vision`` so it can be sent to a
model that reads images.

Free models are uneven, so each role may name its own model. A role without an
override keeps whatever model OS3 sent (the bridge default when it sent none). A
role with an override wins over OS3's model field.

Tool names and the idea of classifying by them come from reading Nesbesss/os3-router
(MIT, ``codex_os3/roles.py``). The code here is this repo's own. Nothing here logs
message text, tool arguments, or keys.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from nexus.config import environ_get, roles_file_path, valid_model_id

logger = logging.getLogger("nexus")

ROLES = ("chat", "worker", "background", "vision")

_CHAT_TOOLS = frozenset({"create_task", "steer_task", "cancel_task", "report_task_on", "render_ui"})
_WORKER_TOOLS = frozenset(
    {
        "computer_use",
        "computer_use_prepare",
        "shell",
        "file_read",
        "file_write",
        "file_edit",
        "feed_image",
        "dummy_system",
    }
)
_BACKGROUND_TOOLS = frozenset(
    {
        "emit_facts",
        "emit_merged_soul",
        "extract_file_signals",
        "report_missed_action",
        "report_correction",
    }
)
_WORKER_PROMPT = re.compile(r"\byou(?:'re| are) an? worker agent\b", re.I)
_IMAGE_PARTS = frozenset({"image_url", "input_image", "image"})
_SETTING_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def classify(payload: dict[str, Any]) -> str:
    """The role of one chat request body: one of ROLES. Pure; never raises."""
    try:
        return _classify(payload)
    except Exception:
        return "chat"


def _classify(payload: dict[str, Any]) -> str:
    tools = _tool_names(payload.get("tools"))
    if _WORKER_PROMPT.search(_system_text(payload.get("messages"))):
        return "worker"
    if tools & _WORKER_TOOLS and not tools & _CHAT_TOOLS:
        return "worker"
    if tools & _CHAT_TOOLS:
        return "chat"
    if tools & _BACKGROUND_TOOLS:
        return "background"
    if tools:
        return "chat"
    return "vision" if _has_image(payload.get("messages")) else "background"


def _tool_names(tools: object) -> set[str]:
    names: set[str] = set()
    for item in tools if isinstance(tools, list) else []:
        fn = item.get("function", item) if isinstance(item, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _system_text(messages: object) -> str:
    parts: list[str] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                part["text"]
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return "\n".join(parts)


def _has_image(messages: object) -> bool:
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in _IMAGE_PARTS for part in content
        ):
            return True
    return False


@dataclass(frozen=True)
class RoleModel:
    """The model one role uses. ``model`` is None when OS3's own model field decides."""

    role: str
    model: str | None
    source: Literal["env", "file", "worker", "os3"]
    setting: str | None = None
    problem: str | None = None


@dataclass(frozen=True)
class RoleConfig:
    roles: dict[str, RoleModel]
    problems: list[str]

    @property
    def configured(self) -> bool:
        return any(item.model is not None for item in self.roles.values()) or bool(self.problems)


def env_name(role: str) -> str:
    return f"NEXUS_MODEL_{role.upper()}"


def _read_file() -> tuple[dict[str, Any], list[str]]:
    path = roles_file_path()
    try:
        if not path.is_file():
            return {}, []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, [f"{path.name} could not be read as JSON; fix or delete it"]
    if not isinstance(data, dict):
        return {}, [f'{path.name} must be a JSON object like {{"worker": "model-id"}}']
    problems = []
    for key in data:
        if key not in ROLES:
            shown = key if _SETTING_RE.fullmatch(key) else "a key"
            problems.append(
                f"{path.name} names an unknown role ({shown}); roles: {', '.join(ROLES)}"
            )
    return data, problems


def role_config() -> RoleConfig:
    """Env first, then the roles file, per role. Read on each call so edits apply live."""
    file_data, problems = _read_file()
    found: dict[str, RoleModel] = {}
    for role in ROLES:
        raw, source, setting = (environ_get(env_name(role)) or "").strip(), "env", env_name(role)
        if not raw:
            value = file_data.get(role)
            raw = value.strip() if isinstance(value, str) else ""
            source, setting = "file", f'"{role}" in {roles_file_path().name}'
            if value is not None and not isinstance(value, str):
                problems.append(f"{setting} must be a model id string")
        if not raw:
            found[role] = RoleModel(role, None, "os3")
        elif valid_model_id(raw):
            found[role] = RoleModel(role, raw, source, setting)
        else:
            found[role] = RoleModel(
                role, None, source, setting, f"{setting} is not a valid model id"
            )
    vision, worker = found["vision"], found["worker"]
    if vision.model is None and vision.problem is None and worker.model is not None:
        found["vision"] = RoleModel("vision", worker.model, "worker", worker.setting)
    problems += [item.problem for item in found.values() if item.problem]
    return RoleConfig(found, problems)


def apply_role_model(payload: dict[str, Any]) -> str:
    """Point ``payload["model"]`` at the role's override, if it has one. Returns the role."""
    role = classify(payload)
    chosen = role_config().roles[role].model
    if chosen is not None and payload.get("model") != chosen:
        payload["model"] = chosen
        logger.info("role=%s model override applied", role)
    return role
