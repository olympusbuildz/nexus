"""Subscription backends. They start the official CLIs and do not read logins.

`codex` runs `codex exec`. `claude-code` runs `claude -p`. `copilot` pipes the
transcript to `copilot -s --no-ask-user`. `grok-build` runs `grok -p`.
`antigravity` runs `agy` with a stream-json prompt on stdin. `/health` and
`/doctor` set `implemented` only after that binary has actually been started.
`ready` is true only when that same probe reports a usable login. A model
list, a version string, or a token variable is not a login.
Probe stdout is not copied into either payload.

`/v1/models` is a fixed catalog, not the account's live list. Client tool
calls are refused: these CLIs do not return caller-defined function calls for
this process to hand back. Inline screenshots are accepted only where the CLI
documents an image path. Codex uses `--image`. Claude Code uses stream-json
image blocks. Copilot uses `--attachment`. Grok Build and Antigravity headless
input is text, so images are 400. Remote image URLs are not fetched. Streaming
forwards CLI events when they arrive; a failure before the first assistant
token stays an HTTP error.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.backends import cli_exec
from nexus.backends.base import ChatUpstream
from nexus.config import (
    ANTIGRAVITY_ENABLE_ENV,
    antigravity_enabled,
    model_for,
    upstream_timeout,
)
from nexus.errors import GatewayError
from nexus.models_meta import NO_UPSTREAM
from nexus.redact import redact

logger = logging.getLogger("nexus")

_PROBE_TIMEOUT = 15.0
_PROBE_TTL = 20.0
_MODEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# The two ways a CLI names the model it resolved for itself. Both are whole-line
# anchors so prose, an email address, or a token cannot be half-matched.
_CLI_DEFAULT_LINE = re.compile(r"(?im)^\s*default\s+model\s*[:\-]\s*(\S+)\s*$")
_CLI_STARRED_DEFAULT = re.compile(r"(?im)^\s*[*\-]\s*(\S+)\s*\(default\)\s*$")
# The header a models-capable CLI prints above its account's model list.
_CLI_MODELS_HEADER = re.compile(r"(?im)^\s*(?:available\s+)?models\s*:\s*$")
# One entry under that header: an optional bullet, the id, an optional
# "(default)" marker, and an optional human description after it.
_CLI_MODELS_ENTRY = re.compile(
    r"^\s*[*\-]?\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})"
    r"(?:\s*\((?:default|current)\))?\s*(?:\s{2,}.*)?$"
)
_CLI_MODELS_MAX = 200

_SYSTEM = (
    "Reply with the assistant message only. Do not use tools, do not edit files, "
    "and do not run commands."
)
_MEDIA_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_MAX_IMAGES = 16
_MAX_IMAGE_BYTES = 12 * 1024 * 1024

CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")
CLAUDE_CODE_MODELS = ("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1")
# Copilot's command reference lists claude-sonnet-4.6 as the general default.
COPILOT_MODELS = ("claude-sonnet-4.6", "gpt-5.4", "claude-haiku-4.5", "gpt-5.3-codex")
GROK_BUILD_MODELS = ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5")
# First slugs from the documented `agy models` sample. Not a live account list.
ANTIGRAVITY_MODELS = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.1-pro-high",
)
# Copilot tool names from the command reference. Removed so a chat turn does
# not run shell, write files, or ask the user. Read tools can still exist.
_COPILOT_EXCLUDED = (
    "bash,powershell,list_bash,list_powershell,read_bash,read_powershell,"
    "stop_bash,stop_powershell,write_bash,write_powershell,apply_patch,create,"
    "edit,task,write_agent,web_fetch,ask_user"
)
_END = object()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_probe_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _dedupe(ids: tuple[str, ...]) -> tuple[str, ...]:
    """First-seen order, no repeats. The bridge's default model leads."""
    seen: set[str] = set()
    out: list[str] = []
    for model_id in ids:
        if model_id and model_id not in seen:
            seen.add(model_id)
            out.append(model_id)
    return tuple(out)


@dataclass
class Feed:
    delta: str = ""
    error: str | None = None


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    incoming = raw.get("input_tokens", raw.get("prompt_tokens", 0))
    outgoing = raw.get("output_tokens", raw.get("completion_tokens", 0))
    return {"input_tokens": _count(incoming), "output_tokens": _count(outgoing)}


def _full(state: dict[str, Any], text: str) -> str:
    have = state["text"]
    if text.startswith(have):
        extra = text[len(have) :]
        state["text"] = text
        return extra
    if have and (have == text or have.endswith(text)):
        return ""
    state["text"] += text
    return text


def feed_event(family: str, event: dict[str, Any], state: dict[str, Any]) -> Feed:
    if family == "codex":
        return _feed_codex(event, state)
    if family == "copilot":
        return _feed_copilot(event, state)
    if family == "grok":
        return _feed_grok(event, state)
    if family == "antigravity":
        return _feed_antigravity(event, state)
    return _feed_claude(event, state)


def _feed_codex(event: dict[str, Any], state: dict[str, Any]) -> Feed:
    kind = event.get("type")
    if kind in {"error", "turn.failed"}:
        message = event.get("message")
        if not isinstance(message, str) or not message.strip():
            err = event.get("error")
            message = err if isinstance(err, str) and err.strip() else "codex turn failed"
        return Feed(error=message)
    if kind == "turn.completed":
        usage = event.get("usage")
        if isinstance(usage, dict):
            state["usage"] = _usage(usage)
        return Feed()
    if kind in {"item.completed", "item.updated"}:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                return Feed(delta=_full(state, text))
    payload = event.get("payload") if kind == "event_msg" else None
    if isinstance(payload, dict) and payload.get("type") in {
        "agent_message_content_delta",
        "agent_message_delta",
    }:
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            state["text"] += delta
            return Feed(delta=delta)
    return Feed()


def _blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
    return "".join(parts)


def _feed_claude(event: dict[str, Any], state: dict[str, Any]) -> Feed:
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event")
        if isinstance(inner, dict) and inner.get("type") == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    state["text"] += text
                    return Feed(delta=text)
        return Feed()
    if kind == "assistant":
        message = event.get("message")
        if isinstance(message, dict):
            text = _blocks_text(message.get("content"))
            if text:
                return Feed(delta=_full(state, text))
        return Feed()
    if kind == "result":
        usage = event.get("usage")
        if isinstance(usage, dict):
            state["usage"] = _usage(usage)
        result = event.get("result")
        if event.get("is_error"):
            message = (
                result if isinstance(result, str) and result.strip() else "claude reported an error"
            )
            return Feed(error=message)
        if isinstance(result, str):
            return Feed(delta=_full(state, result))
    return Feed()


def _data(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("data")
    if isinstance(raw, dict):
        return raw
    return event


def _feed_copilot(event: dict[str, Any], state: dict[str, Any]) -> Feed:
    kind = event.get("type")
    data = _data(event)
    if kind in {"error", "session.error"}:
        message = data.get("message") or data.get("error") or event.get("message")
        if not isinstance(message, str) or not message.strip():
            message = "copilot turn failed"
        return Feed(error=message)
    if kind == "assistant.message_delta":
        delta = data.get("deltaContent", data.get("delta_content", data.get("delta")))
        if isinstance(delta, str) and delta:
            state["text"] += delta
            return Feed(delta=delta)
        return Feed()
    if kind == "assistant.message":
        usage = data.get("usage")
        if isinstance(usage, dict):
            state["usage"] = _usage(usage)
        text = data.get("content")
        if isinstance(text, str):
            return Feed(delta=_full(state, text))
        return Feed()
    if kind in {"result", "session.idle"}:
        usage = data.get("usage")
        if isinstance(usage, dict):
            state["usage"] = _usage(usage)
    return Feed()


def _feed_grok(event: dict[str, Any], state: dict[str, Any]) -> Feed:
    kind = event.get("type")
    if kind == "error" or event.get("is_error") is True:
        message = event.get("message") or event.get("error")
        if not isinstance(message, str) or not message.strip():
            message = "grok turn failed"
        return Feed(error=message)
    if kind == "text":
        data = event.get("data")
        if isinstance(data, str) and data:
            state["text"] += data
            return Feed(delta=data)
        return Feed()
    if kind in {"usage", "end"}:
        usage = event.get("usage")
        if isinstance(usage, dict):
            state["usage"] = _usage(usage)
        return Feed()
    text = event.get("text")
    if kind is None and isinstance(text, str):
        usage = event.get("usage")
        if isinstance(usage, dict):
            state["usage"] = _usage(usage)
        return Feed(delta=_full(state, text))
    return Feed()


def _feed_antigravity(event: dict[str, Any], state: dict[str, Any]) -> Feed:
    kind = event.get("event")
    if kind == "step_update":
        step = event.get("step_update")
        if (
            isinstance(step, dict)
            and step.get("step_type") == "agent_response"
            and isinstance(step.get("text_delta"), str)
            and step["text_delta"]
        ):
            state["text"] += step["text_delta"]
            return Feed(delta=step["text_delta"])
        return Feed()
    nested = event.get("result")
    body = nested if kind == "result" and isinstance(nested, dict) else None
    if body is None and kind is None and "status" in event and "response" in event:
        body = event
    if not isinstance(body, dict):
        return Feed()
    usage = body.get("usage")
    if isinstance(usage, dict):
        state["usage"] = _usage(usage)
    status = body.get("status")
    if isinstance(status, str) and status.upper() not in {"", "SUCCESS"}:
        message = body.get("error")
        if not isinstance(message, str) or not message.strip():
            message = f"antigravity status {status}"
        return Feed(error=message)
    response = body.get("response")
    if isinstance(response, str):
        return Feed(delta=_full(state, response))
    return Feed()


def _events(stdout: str) -> list[dict[str, Any]]:
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return [payload]
    found: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def parse_cli_stdout(family: str, stdout: str) -> tuple[str, dict[str, int], str | None]:
    state: dict[str, Any] = {"text": "", "usage": {}}
    error: str | None = None
    for event in _events(stdout):
        feed = feed_event(family, event, state)
        if feed.error:
            error = feed.error
    usage = state["usage"] if isinstance(state["usage"], dict) else {}
    return state["text"], usage, error


@dataclass(frozen=True)
class InlineImage:
    media_type: str
    data: str


@dataclass(frozen=True)
class CliTurn:
    stdin: str
    image_paths: tuple[str, ...]
    stream_json_input: bool


def _image_mode(family: str) -> str:
    if family == "claude":
        return "blocks"
    if family in {"codex", "copilot"}:
        return "files"
    return "refuse"


def build_turn(payload: dict[str, Any], *, preamble: bool, family: str, cwd: str) -> CliTurn:
    """One chat body as CLI stdin. Text-only stdin matches the plain transcript."""
    if payload.get("tools") or payload.get("functions") or payload.get("tool_choice"):
        raise _unsupported_tools()
    image_mode = _image_mode(family)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(
            400,
            "messages must be a non-empty list",
            "invalid_request_error",
            "invalid_messages",
        )
    text_blocks: list[str] = []
    claude_blocks: list[dict[str, Any]] = []
    images: list[InlineImage] = []
    if preamble:
        text_blocks.append(_SYSTEM)
    for message in messages:
        if not isinstance(message, dict):
            raise GatewayError(
                400,
                "messages must be a list of objects",
                "invalid_request_error",
                "invalid_messages",
            )
        role = message.get("role")
        if role == "tool" or message.get("tool_calls") or message.get("function_call"):
            raise _unsupported_tools()
        if role not in {"system", "user", "assistant", "developer"}:
            raise GatewayError(
                400,
                "message role must be system, user, assistant, or developer",
                "invalid_request_error",
                "invalid_messages",
            )
        pieces = _split_content(message.get("content"), image_mode=image_mode)
        shown: list[str] = []
        labeled = False
        for piece in pieces:
            if isinstance(piece, InlineImage):
                images.append(piece)
                shown.append("[image]")
                claude_blocks.append(_claude_image(piece))
                continue
            shown.append(piece)
            if not labeled:
                claude_blocks.append({"type": "text", "text": f"{role}:\n{piece}"})
                labeled = True
            elif piece:
                claude_blocks.append({"type": "text", "text": piece})
        text_blocks.append(f"{role}:\n" + "\n".join(shown))
    if len(images) > _MAX_IMAGES:
        raise _invalid_image(f"at most {_MAX_IMAGES} images per request")
    prompt = "\n\n".join(text_blocks)
    if family == "grok":
        _write_prompt(cwd, prompt)
        return CliTurn(stdin="", image_paths=(), stream_json_input=False)
    if family == "antigravity":
        stdin = json.dumps(
            {"event": "user", "message": {"content": prompt}},
            ensure_ascii=False,
        )
        return CliTurn(stdin=stdin + "\n", image_paths=(), stream_json_input=True)
    if family == "claude" and images:
        if not claude_blocks:
            claude_blocks.append({"type": "text", "text": "Describe the attached image."})
        stdin = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": claude_blocks},
                "parent_tool_use_id": None,
            },
            ensure_ascii=False,
        )
        return CliTurn(stdin=stdin + "\n", image_paths=(), stream_json_input=True)
    paths = tuple(_write_images(images, cwd)) if images else ()
    return CliTurn(stdin=prompt, image_paths=paths, stream_json_input=False)


def _split_content(content: object, *, image_mode: str) -> list[str | InlineImage]:
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise _unsupported_content()
    pieces: list[str | InlineImage] = []
    for part in content:
        if isinstance(part, str):
            pieces.append(part)
            continue
        if not isinstance(part, dict):
            raise _unsupported_content()
        kind = part.get("type")
        if kind in {"image", "image_url", "input_image"}:
            if image_mode == "refuse":
                raise _unsupported_images()
            pieces.append(_inline_image(part))
            continue
        if kind in {None, "text"} and isinstance(part.get("text"), str):
            pieces.append(part["text"])
            continue
        raise _unsupported_content()
    return pieces


def _inline_image(part: dict[str, Any]) -> InlineImage:
    kind = part.get("type")
    if kind in {"image_url", "input_image"}:
        return _image_from_url(_image_url_text(part.get("image_url")))
    source = part.get("source")
    if not isinstance(source, dict):
        raise _invalid_image("image.source is required")
    source_type = source.get("type")
    if source_type == "base64":
        media = source.get("media_type")
        data = source.get("data")
        if not isinstance(media, str) or not media or not isinstance(data, str) or not data:
            raise _invalid_image("image base64 source needs media_type and data")
        return _image_from_url(f"data:{media};base64,{data}")
    if source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _invalid_image("image url source needs url")
        return _image_from_url(url)
    raise _invalid_image("image.source.type must be base64 or url")


def _image_url_text(raw: object) -> str:
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("url"), str) and raw["url"]:
        return raw["url"]
    raise _invalid_image("image_url is required")


def _image_from_url(url: str) -> InlineImage:
    if not url.startswith("data:"):
        raise _remote_image()
    header, sep, payload = url.partition(",")
    if sep != ",":
        raise _invalid_image("image data URL must be data:<media>;base64,<data>")
    meta = header[5:]
    media, semi, encoding = meta.partition(";")
    if semi != ";" or encoding.lower() != "base64":
        raise _invalid_image("image data URL must be data:<media>;base64,<data>")
    if media not in _MEDIA_EXT:
        raise _invalid_image("image media_type must be png, jpeg, gif, or webp")
    compact = "".join(payload.split())
    if not compact:
        raise _invalid_image("image base64 is empty")
    try:
        raw = base64.b64decode(compact, validate=True)
    except ValueError:
        raise _invalid_image("image base64 is invalid") from None
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise _invalid_image("image is empty or larger than 12 MiB")
    return InlineImage(media_type=media, data=compact)


def _write_prompt(cwd: str, prompt: str) -> None:
    path = Path(cwd) / "prompt.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, prompt.encode("utf-8"))
    finally:
        os.close(fd)


def _write_images(images: list[InlineImage], cwd: str) -> list[str]:
    paths: list[str] = []
    for index, image in enumerate(images, start=1):
        path = Path(cwd) / f"shot-{index}.{_MEDIA_EXT[image.media_type]}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, base64.b64decode(image.data))
        finally:
            os.close(fd)
        paths.append(str(path))
    return paths


def _claude_image(image: InlineImage) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": image.media_type, "data": image.data},
    }


def _unsupported_images() -> GatewayError:
    return GatewayError(
        400,
        "Inline images are not supported on this subscription CLI.",
        "invalid_request_error",
        "images_not_supported",
    )


def _unsupported_tools() -> GatewayError:
    return GatewayError(
        400,
        "Tool calls are not supported on subscription CLI backends.",
        "invalid_request_error",
        "tools_not_supported",
    )


def _remote_image() -> GatewayError:
    return GatewayError(
        400,
        (
            "Subscription CLIs accept inline base64 images only. "
            "This bridge does not fetch remote image URLs."
        ),
        "invalid_request_error",
        "image_url_unsupported",
    )


def _invalid_image(message: str) -> GatewayError:
    return GatewayError(400, message, "invalid_request_error", "invalid_image")


def _unsupported_content() -> GatewayError:
    return GatewayError(
        400,
        "Only text content is supported on subscription CLI backends.",
        "invalid_request_error",
        "unsupported_content",
    )


def _require_model(raw: object, default: str) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        model = default
    elif isinstance(raw, str):
        model = raw.strip()
    else:
        raise GatewayError(
            400,
            "model must be a string",
            "invalid_request_error",
            "invalid_model",
        )
    if _MODEL_OK.fullmatch(model) is None:
        raise GatewayError(
            400,
            "model must be a single token of letters, numbers, and . _ : -",
            "invalid_request_error",
            "invalid_model",
        )
    return model


def _public_error(text: str) -> str:
    cleaned = _EMAIL.sub("[redacted-email]", redact(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:400]


def _completion(model: str, text: str, usage: dict[str, int]) -> bytes:
    prompt = _count(usage.get("input_tokens"))
    completion = _count(usage.get("output_tokens"))
    body = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _sse(
    model: str,
    *,
    completion_id: str,
    created: int,
    delta: dict[str, Any],
    finish: str | None,
) -> bytes:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


class SubscriptionBackend:
    id = ""
    family = ""
    command = ""
    bin_env = ""
    label = ""
    login_command = ""
    probe_args: tuple[str, ...] = ()
    model_args: tuple[str, ...] = ()
    fixed_models: tuple[str, ...] = ()
    enumerates_models = False
    preamble = True

    def listing_args(self) -> str:
        """The command a model list, when there is one, came from."""
        return " ".join(self.model_args or self.probe_args)

    def fixed_catalog(self) -> tuple[str, ...]:
        """Every model id this backend offers from the bridge's own catalog."""
        return _dedupe((model_for(self.id), *self.fixed_models))

    async def model_reachable(self, model: str) -> bool | None:
        """A CLI resolves the model itself, so a list check would not be honest."""
        return None

    def describe(self) -> dict[str, Any]:
        binary = cli_exec.resolve_binary(self.bin_env, self.command)
        key = f"{self.id}:{binary}"
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and now - hit[0] < _PROBE_TTL:
                return self._with_catalog(hit[1])
        payload = self._probe(binary)
        with _cache_lock:
            _cache[key] = (time.monotonic(), payload)
        return self._with_catalog(payload)

    def _with_catalog(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add the model list to a cached probe, without running anything.

        The dashboard polls /health every few seconds. The probe already ran,
        and its answer is in the cache; the catalog is derived from that
        answer so a refresh never starts a second CLI process.
        """
        row = dict(payload)
        live = row.get("discovered_models") or ()
        listed = f"`{self.command} {self.listing_args()}`"
        if live:
            row["models"] = list(live)
            row["models_source"] = "cli"
            row["models_hint"] = f"Enumerated from {listed}, the list the account reported."
        else:
            catalog = self.fixed_catalog()
            row["models"] = list(catalog)
            if self.enumerates_models:
                row["models_source"] = "catalog"
                row["models_hint"] = (
                    f"{listed} did not report a model "
                    "list. These are the bridge's own catalog, not the account's."
                )
            else:
                row["models_source"] = "catalog"
                row["models_hint"] = (
                    f"`{self.command}` has no model-listing command, so the account's "
                    "entitlements cannot be enumerated. These are the bridge's own "
                    "catalog, not the account's."
                )
        return row

    def _probe(self, binary: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.label,
            "role": "subscription",
            "kind": "cli",
            "command": self.command,
            "implemented": False,
            "ready": False,
            "cli_present": binary is not None,
            "cli_path": binary,
            "login": "unchecked",
            "probe": " ".join(self.probe_args),
            "default_model": model_for(self.id),
            "cli_model": None,
            "credential": "cli-login",
            "hint": (
                f"{self.command} CLI not found. Install the official CLI and sign in there. "
                "This bridge does not store login tokens."
            ),
        }
        if binary is None:
            return payload
        try:
            result = cli_exec.run_sync(
                [binary, *self.probe_args],
                stdin="",
                timeout=_PROBE_TIMEOUT,
                env=cli_exec.child_env(binary),
                cwd=None,
            )
        except cli_exec.CliSpawnError:
            payload["hint"] = f"Could not start {self.command}."
            return payload
        if not result.started:
            return payload
        login = self._login_state(result)
        payload["implemented"] = True
        payload["login"] = login
        payload["ready"] = login == "ok"
        payload["hint"] = _login_hint(self.login_command, login)
        payload["cli_model"] = _cli_resolved_model(f"{result.stdout}\n{result.stderr}")
        if self.enumerates_models:
            discovered = _cli_model_list(f"{result.stdout}\n{result.stderr}")
            if self.model_args:
                discovered = self._probe_models(binary) or discovered
            if discovered:
                payload["discovered_models"] = list(discovered)
        return payload

    def _probe_models(self, binary: str) -> tuple[str, ...]:
        """The model ids this CLI's own listing command reported, or ().

        A second command inside the same probe cycle: its answer goes into the
        one cached payload, so a /health poll still starts no extra process.
        Anything the command refuses to say is simply not a model.
        """
        try:
            result = cli_exec.run_sync(
                [binary, *self.model_args],
                stdin="",
                timeout=_PROBE_TIMEOUT,
                env=cli_exec.child_env(binary),
                cwd=None,
            )
        except cli_exec.CliSpawnError:
            return ()
        if not result.started:
            return ()
        return _cli_model_slugs(f"{result.stdout}\n{result.stderr}")

    def _login_state(self, result: cli_exec.CliResult) -> str:
        return "unusable"

    async def list_models(self) -> list[dict[str, Any]]:
        now = int(time.time())
        row = await asyncio.to_thread(self.describe)
        ids: tuple[str, ...] = tuple(row.get("models") or ())
        live = bool(row.get("discovered_models"))
        rows: list[dict[str, Any]] = []
        for model_id in ids:
            entry: dict[str, Any] = {
                "id": model_id,
                "object": "model",
                "created": now,
                "owned_by": self.id,
            }
            if not live:
                entry["warning"] = f"unverified: {NO_UPSTREAM}"
            rows.append(entry)
        return rows

    def argv(
        self,
        binary: str,
        model: str,
        cwd: str,
        *,
        stream: bool,
        image_paths: list[str],
        stream_json_input: bool,
    ) -> list[str]:
        del binary, model, cwd, stream, image_paths, stream_json_input
        raise NotImplementedError

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        binary = cli_exec.resolve_binary(self.bin_env, self.command)
        if binary is None:
            raise self._missing_cli()
        model = _require_model(payload.get("model"), model_for(self.id))
        cwd = tempfile.mkdtemp(prefix="os3-cli-")
        handed_off = False
        try:
            turn = build_turn(payload, preamble=self.preamble, family=self.family, cwd=cwd)
            argv = self.argv(
                binary,
                model,
                cwd,
                stream=stream,
                image_paths=list(turn.image_paths),
                stream_json_input=turn.stream_json_input,
            )
            env = cli_exec.child_env(binary)
            if stream:
                upstream = await self._open_stream(argv, turn.stdin, env, cwd, model)
                handed_off = True
                return upstream
            try:
                result = await cli_exec.run_async(
                    argv,
                    stdin=turn.stdin,
                    timeout=upstream_timeout(),
                    env=env,
                    cwd=cwd,
                )
            except cli_exec.CliSpawnError:
                raise self._missing_cli() from None
            text, usage, error = parse_cli_stdout(self.family, result.stdout)
            if not text:
                raise self._failure(result.returncode, result.stderr, result.stdout, error)
            return ChatUpstream(
                status_code=200,
                media_type="application/json",
                body=_completion(model, text, usage),
            )
        finally:
            if not handed_off:
                shutil.rmtree(cwd, ignore_errors=True)

    async def _open_stream(
        self,
        argv: list[str],
        prompt: str,
        env: dict[str, str],
        cwd: str,
        model: str,
    ) -> ChatUpstream:
        try:
            proc = await cli_exec.start(
                argv,
                stdin=prompt,
                timeout=upstream_timeout(),
                env=env,
                cwd=cwd,
            )
        except cli_exec.CliSpawnError:
            shutil.rmtree(cwd, ignore_errors=True)
            raise self._missing_cli() from None
        state: dict[str, Any] = {"text": "", "usage": {}}
        queue: asyncio.Queue[object] = asyncio.Queue()

        async def pump() -> None:
            parsed_error: str | None = None
            code = 1
            stderr = ""
            try:
                async for line in proc.lines():
                    piece = line.strip()
                    if not piece:
                        continue
                    try:
                        event = json.loads(piece)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    feed = feed_event(self.family, event, state)
                    if feed.error:
                        parsed_error = feed.error
                        if not state["text"]:
                            await queue.put(self._failure(1, "", "", feed.error))
                            return
                        break
                    if feed.delta:
                        await queue.put(feed.delta)
                code, stderr = await proc.finish()
            except GatewayError as exc:
                await queue.put(exc)
                return
            except cli_exec.CliSpawnError:
                await queue.put(self._missing_cli())
                return
            except Exception as exc:
                logger.error("cli stream failed: %s", type(exc).__name__)
                await queue.put(
                    GatewayError(
                        502,
                        f"{self.label} CLI failed",
                        "api_error",
                        f"{self.id}_failed",
                    )
                )
                return
            if not state["text"]:
                await queue.put(self._failure(code, stderr, "", parsed_error))
                return
            await queue.put(_END)

        task = asyncio.create_task(pump())
        try:
            first = await queue.get()
        except BaseException:
            task.cancel()
            await proc.kill()
            shutil.rmtree(cwd, ignore_errors=True)
            await _finish_task(task)
            raise
        if not isinstance(first, str):
            await _finish_task(task)
            await proc.kill()
            shutil.rmtree(cwd, ignore_errors=True)
            if isinstance(first, GatewayError):
                raise first
            raise self._failure(1, "", "", "CLI returned no assistant text")

        completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        async def gen() -> AsyncIterator[bytes]:
            try:
                yield _sse(
                    model,
                    completion_id=completion_id,
                    created=created,
                    delta={"role": "assistant", "content": first},
                    finish=None,
                )
                while True:
                    item = await queue.get()
                    if item is _END or isinstance(item, GatewayError):
                        break
                    if isinstance(item, str) and item:
                        yield _sse(
                            model,
                            completion_id=completion_id,
                            created=created,
                            delta={"content": item},
                            finish=None,
                        )
                yield _sse(
                    model,
                    completion_id=completion_id,
                    created=created,
                    delta={},
                    finish="stop",
                )
                yield b"data: [DONE]\n\n"
            finally:
                task.cancel()
                await proc.kill()
                shutil.rmtree(cwd, ignore_errors=True)
                await _finish_task(task)

        return ChatUpstream(status_code=200, media_type="text/event-stream", stream=gen())

    def _missing_cli(self) -> GatewayError:
        return GatewayError(
            503,
            (
                f"{self.label} CLI is not installed or {self.bin_env} is not an executable. "
                "This bridge does not store login tokens."
            ),
            f"{self.id}_unavailable",
            f"{self.id}_cli_missing",
        )

    def _failure(
        self, code: int, stderr: str, stdout: str, parsed_error: str | None
    ) -> GatewayError:
        blob = "\n".join(part for part in (parsed_error or "", stderr, stdout) if part).strip()
        lower = blob.lower()
        if any(
            phrase in lower
            for phrase in (
                "not logged in",
                "not signed in",
                "not authenticated",
                "authentication required",
                "please log in",
                "please sign in",
                "login required",
                "/login",
            )
        ):
            return GatewayError(
                503,
                (
                    f"{self.label} CLI is not logged in. Run `{self.login_command}` in a terminal. "
                    "This bridge does not store that login."
                ),
                f"{self.id}_unavailable",
                f"{self.id}_login_required",
            )
        if "usage limit" in lower or ("rate limit" in lower and "subscription" in lower):
            detail = _public_error(blob) or "subscription usage limit reached"
            return GatewayError(
                429,
                detail,
                "rate_limit_error",
                f"{self.id}_usage_limit",
            )
        if code == 124 or "timed out" in lower:
            return GatewayError(
                504,
                f"{self.label} CLI timed out",
                "api_error",
                f"{self.id}_timeout",
            )
        detail = _public_error(blob)
        if not detail:
            return GatewayError(
                502,
                f"{self.label} CLI returned no assistant text.",
                "api_error",
                f"{self.id}_empty",
            )
        return GatewayError(502, detail, "api_error", f"{self.id}_failed")


async def _finish_task(task: asyncio.Task[None]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _cli_resolved_model(text: str) -> str | None:
    """The model a CLI says it resolved for itself, or None.

    Read from probe output the probe already ran, so no command runs twice. Two
    shapes are accepted: a `Default model: <id>` line (`grok models`) and a
    `* <id> (default)` list entry (`agy models`). A candidate must look like a
    model id and must not have been redacted or an email; otherwise it is not
    reported. Returned as a single token, never as a line of CLI text.
    """
    for pattern in (_CLI_DEFAULT_LINE, _CLI_STARRED_DEFAULT):
        for match in pattern.finditer(text):
            candidate = match.group(1).strip().strip("\"'`,")
            if _MODEL_OK.fullmatch(candidate) is None:
                continue
            if "@" in candidate or candidate.lower().startswith("["):
                continue
            return candidate
    return None


def _cli_model_list(text: str) -> tuple[str, ...]:
    """The account model ids a `models` probe already printed, or ().

    Only the output of the command the probe ran is read here. A CLI whose
    probe is `login status` or `version` prints no list, so it returns () and
    its row stays on the fixed catalog rather than being invented. Ids are
    taken from under a `Models:` header only, so login prose above the header
    cannot be mistaken for a model. Order is the CLI's own; duplicates and
    redaction markers are dropped.
    """
    header = _CLI_MODELS_HEADER.search(text)
    if header is None:
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for line in text[header.end() :].splitlines():
        if not line.strip():
            continue
        if _EMAIL.search(line) or "[REDACTED" in line:
            continue
        match = _CLI_MODELS_ENTRY.match(line)
        if match is None:
            break
        candidate = match.group(1).strip()
        if candidate in seen:
            continue
        if candidate.lower().startswith(("[", "redacted")):
            continue
        seen.add(candidate)
        found.append(candidate)
        if len(found) >= _CLI_MODELS_MAX:
            break
    return tuple(found)


def _cli_model_slugs(text: str) -> tuple[str, ...]:
    """The `slug` values in a `{"models": [...]}` listing, or ().

    Some CLIs answer a model listing with JSON instead of a printed table. Only
    a real JSON object carrying a `models` array of objects is read; anything
    else, and any line that merely mentions "models", is not a list. Ids must be
    single model tokens, order is the CLI's own, and duplicates collapse.
    """
    text = text.strip()
    if not text.startswith("{"):
        return ()
    try:
        parsed = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(parsed, dict):
        return ()
    entries = parsed.get("models")
    if not isinstance(entries, list):
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str):
            continue
        candidate = slug.strip()
        if _MODEL_OK.fullmatch(candidate) is None:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
        if len(found) >= _CLI_MODELS_MAX:
            break
    return tuple(found)


def _login_hint(command: str, login: str) -> str:
    if login == "ok":
        return "ok"
    if login == "missing":
        return (
            f"CLI is not logged in. Run `{command}` in a terminal. "
            "This bridge does not read or store that login."
        )
    if login == "unknown":
        return (
            f"No login-status command. Sign in with `{command}` in a terminal. "
            "This probe does not know whether that CLI is signed in, and it "
            "does not read or store that login."
        )
    return "Login probe did not report a usable login. Probe output is not included."


class CodexCliBackend(SubscriptionBackend):
    id = "codex"
    family = "codex"
    command = "codex"
    bin_env = "NEXUS_CODEX_BIN"
    label = "Codex"
    login_command = "codex login"
    probe_args = ("login", "status")
    model_args = ("debug", "models")
    enumerates_models = True
    fixed_models = CODEX_MODELS
    preamble = True

    def _login_state(self, result: cli_exec.CliResult) -> str:
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "not logged in" in text or "login required" in text or "not authenticated" in text:
            return "missing"
        if result.returncode == 0 and "logged in" in text:
            return "ok"
        return "unusable"

    def argv(
        self,
        binary: str,
        model: str,
        cwd: str,
        *,
        stream: bool,
        image_paths: list[str],
        stream_json_input: bool,
    ) -> list[str]:
        del stream, stream_json_input
        argv = [
            binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-s",
            "read-only",
            # `codex exec` has no `-a` flag (CLI 0.162.1 rejects it); set the
            # approval policy through a config override instead.
            "-c",
            'approval_policy="never"',
            "-C",
            cwd,
            "-m",
            model,
        ]
        for path in image_paths:
            argv.extend(["--image", path])
        argv.extend(["--", "-"])
        return argv


class ClaudeCodeBackend(SubscriptionBackend):
    id = "claude-code"
    family = "claude"
    command = "claude"
    bin_env = "NEXUS_CLAUDE_BIN"
    label = "Claude Code"
    login_command = "claude auth login"
    probe_args = ("auth", "status")
    fixed_models = CLAUDE_CODE_MODELS
    preamble = False

    def _login_state(self, result: cli_exec.CliResult) -> str:
        try:
            payload = json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("loggedIn"), bool):
            return "ok" if payload["loggedIn"] else "missing"
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "not logged" in text or "not authenticated" in text:
            return "missing"
        return "unusable"

    def argv(
        self,
        binary: str,
        model: str,
        cwd: str,
        *,
        stream: bool,
        image_paths: list[str],
        stream_json_input: bool,
    ) -> list[str]:
        del cwd, image_paths
        output = "stream-json" if stream else "json"
        argv = [
            binary,
            "-p",
            "--model",
            model,
            "--output-format",
            output,
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources",
            "user",
            "--system-prompt",
            _SYSTEM,
        ]
        if stream_json_input:
            argv.extend(["--input-format", "stream-json"])
        if stream:
            argv.extend(["--verbose", "--include-partial-messages"])
        return argv


def _logged_out(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "not logged in",
            "not signed in",
            "not authenticated",
            "unauthenticated",
            "authentication required",
            "please log in",
            "please sign in",
            "login required",
        )
    )


def _login_from_probe(result: cli_exec.CliResult) -> str:
    """Login comes from the probe text. Exit 0 and a model list are not enough."""
    text = f"{result.stdout}\n{result.stderr}".lower()
    if _logged_out(text):
        return "missing"
    if "logged in" in text or "signed in" in text:
        return "ok"
    return "unusable"


class CopilotCliBackend(SubscriptionBackend):
    """GitHub Copilot CLI. Prompt is piped. `-p` would put that text on argv.

    The programmatic guide accepts either `copilot -p "..."` or a pipe. A pipe
    is ignored when `-p` is also set, so this process uses the pipe form with
    `-s` and `--no-ask-user`. The binary is `copilot`, not `gh`. `gh auth status`
    and `/user` describe GitHub CLI auth and are not this probe. `copilot version`
    proves the Copilot CLI started. `ready` needs that output to say the user is
    logged in. Token variables are not inspected.
    """

    id = "copilot"
    family = "copilot"
    command = "copilot"
    bin_env = "NEXUS_COPILOT_BIN"
    label = "GitHub Copilot"
    login_command = "copilot login"
    probe_args = ("version",)
    fixed_models = COPILOT_MODELS
    preamble = True

    def _login_state(self, result: cli_exec.CliResult) -> str:
        return _login_from_probe(result)

    def argv(
        self,
        binary: str,
        model: str,
        cwd: str,
        *,
        stream: bool,
        image_paths: list[str],
        stream_json_input: bool,
    ) -> list[str]:
        del cwd, stream, stream_json_input
        argv = [
            binary,
            "-s",
            "--no-ask-user",
            "--no-color",
            "--no-custom-instructions",
            "--output-format",
            "json",
            "--model",
            model,
            "--excluded-tools",
            _COPILOT_EXCLUDED,
        ]
        for path in image_paths:
            argv.extend(["--attachment", path])
        return argv


class GrokBuildBackend(SubscriptionBackend):
    """xAI Grok Build CLI. Distinct from the API-key `xai` backend.

    The CLI reference one-shot is `grok -p`. The prompt is that argument.
    `-m` selects the model. Streaming uses `streaming-json`. `--sandbox
    read-only` is the documented profile that blocks child network and limits
    writes. There is no status or whoami command. The dry probe is
    `grok models`, which lists models and does not start a completion. It
    prints `You are not authenticated.` and still exits 0 when there is no
    login, so that line is the login signal: it is `missing`, and its absence
    is `ok`. `grok --version` only printed a version and could never say.
    """

    id = "grok-build"
    family = "grok"
    command = "grok"
    bin_env = "NEXUS_GROK_BIN"
    label = "Grok Build"
    login_command = "grok login"
    probe_args = ("models",)
    fixed_models = GROK_BUILD_MODELS
    enumerates_models = True
    preamble = True

    def _login_state(self, result: cli_exec.CliResult) -> str:
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "not authenticated" in text or "not logged in" in text:
            return "missing"
        if "grok login" in text and "logged in" not in text:
            return "missing"
        # `grok models` is the only probe that states the login. Without the
        # unauthenticated line the CLI is signed in.
        return "ok"

    def argv(
        self,
        binary: str,
        model: str,
        cwd: str,
        *,
        stream: bool,
        image_paths: list[str],
        stream_json_input: bool,
    ) -> list[str]:
        del image_paths, stream_json_input
        output = "streaming-json" if stream else "json"
        prompt = (Path(cwd) / "prompt.txt").read_text(encoding="utf-8")
        return [
            binary,
            "-p",
            prompt,
            "--output-format",
            output,
            "-m",
            model,
            "--cwd",
            cwd,
            "--sandbox",
            "read-only",
            "--max-turns",
            "1",
            "--no-auto-update",
        ]


class AntigravityBackend(SubscriptionBackend):
    """Google Antigravity CLI (`agy`). Not the retired consumer Gemini CLI.

    `-p` takes the prompt as an argument. Headless stdin is
    `--input-format stream-json` (a `user` event), and that input requires
    `--output-format stream-json`. The published install guide signs in by
    starting `agy` (keyring, browser, or the URL it prints over SSH). There
    is no `agy auth login`. `agy models` starts the binary and does not start
    a completion. A model list is not a login.
    """

    id = "antigravity"
    family = "antigravity"
    command = "agy"
    bin_env = "NEXUS_AGY_BIN"
    label = "Antigravity"
    login_command = "agy"
    probe_args = ("models",)
    fixed_models = ANTIGRAVITY_MODELS
    enumerates_models = True
    preamble = True

    def _disabled_hint(self) -> str:
        return (
            "Antigravity is off by default: it carries higher terms-of-service risk. "
            f"Set {ANTIGRAVITY_ENABLE_ENV}=1 to opt in. "
            "See 'Provider risk' in the README."
        )

    def describe(self) -> dict[str, Any]:
        if antigravity_enabled():
            return super().describe()
        payload = self._probe(None)
        payload["cli_present"] = cli_exec.resolve_binary(self.bin_env, self.command) is not None
        payload["enabled"] = False
        payload["hint"] = self._disabled_hint()
        return self._with_catalog(payload)

    async def open_chat(self, payload: dict[str, Any], *, stream: bool) -> ChatUpstream:
        if not antigravity_enabled():
            raise GatewayError(
                403,
                f"The antigravity backend is disabled. Set {ANTIGRAVITY_ENABLE_ENV}=1 to opt in.",
                "invalid_request_error",
                "backend_disabled",
                extra={"hint": self._disabled_hint()},
            )
        return await super().open_chat(payload, stream=stream)

    def _login_state(self, result: cli_exec.CliResult) -> str:
        return _login_from_probe(result)

    def argv(
        self,
        binary: str,
        model: str,
        cwd: str,
        *,
        stream: bool,
        image_paths: list[str],
        stream_json_input: bool,
    ) -> list[str]:
        del cwd, stream, image_paths, stream_json_input
        return [
            binary,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--sandbox",
        ]
