"""POST /v1/images/generations: OpenAI Images in, a plan's image model out.

chatgpt (default when the Codex ChatGPT login is present): the plan's own Images
endpoint, ``{base}/images/generations`` with ``gpt-image-2``, the way the openai/codex
image-generation extension calls it. On 404 or an "unsupported" refusal it falls back to
``/responses`` with the hosted ``image_generation`` tool. A usage limit is 429
``chatgpt_quota``, as for chat. At your own risk, like the chatgpt chat backend.

nous: off unless ``NEXUS_ENABLE_NOUS_IMAGES=1``. Billed per image by Nous, so it is
capped (``NEXUS_NOUS_IMAGE_DAILY_CAP``, default 10 per rolling 24 h). Uses chat
completions with an image-output model and pulls the image out of the reply.

Prompts are never logged. Tokens are never logged or returned.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from typing import Any

import httpx

from nexus import chatgpt_auth
from nexus.config import environ_get, upstream_timeout
from nexus.errors import GatewayError
from nexus.redact import redact

CHATGPT_IMAGE_MODEL = "gpt-image-2"
NOUS_IMAGE_MODEL = "google/gemini-nano-banana-2.1"
NOUS_ENABLE_ENV = "NEXUS_ENABLE_NOUS_IMAGES"
NOUS_CAP_ENV = "NEXUS_NOUS_IMAGE_DAILY_CAP"
IMAGES_BACKEND_ENV = "NEXUS_IMAGES_BACKEND"
DEFAULT_NOUS_CAP = 10
_DAY_S = 86400.0
_MAX_PROMPT = 32000
_MAX_N = 4
_SIZE = re.compile(r"^(auto|\d{3,4}x\d{3,4})$")
_QUALITY = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "auto": "auto",
    "standard": "auto",
    "hd": "high",
}
_BACKGROUND = {"auto", "opaque", "transparent"}
_FORMATS = {"png", "jpeg", "webp"}
_RESPONSES_MODEL_ENV = "NEXUS_IMAGES_RESPONSES_MODEL"
_ON = {"1", "on", "true", "yes"}


def _bad(message: str, code: str = "invalid_image_request") -> GatewayError:
    return GatewayError(400, message, "invalid_request_error", code)


def parse_request(raw: bytes) -> dict[str, Any]:
    """Validate an OpenAI Images body. Returns the normalized fields."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise _bad("Body must be JSON.") from None
    if not isinstance(body, dict):
        raise _bad("Body must be a JSON object.")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _bad("prompt is required.", "prompt_required")
    if len(prompt) > _MAX_PROMPT:
        raise _bad(f"prompt is longer than {_MAX_PROMPT} characters.")
    n = body.get("n", 1)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= _MAX_N:
        raise _bad(f"n must be an integer from 1 to {_MAX_N}.")
    size = body.get("size") or "auto"
    if not isinstance(size, str) or not _SIZE.match(size):
        raise _bad("size must be 'auto' or WIDTHxHEIGHT, for example 1024x1024.")
    quality = _QUALITY.get(str(body.get("quality") or "auto").lower())
    if quality is None:
        raise _bad("quality must be low, medium, high or auto.")
    background = str(body.get("background") or "auto").lower()
    if background not in _BACKGROUND:
        raise _bad("background must be auto, opaque or transparent.")
    output_format = str(body.get("output_format") or "png").lower()
    if output_format not in _FORMATS:
        raise _bad("output_format must be png, jpeg or webp.")
    response_format = str(body.get("response_format") or "b64_json").lower()
    if response_format not in {"b64_json", "url"}:
        raise _bad("response_format must be b64_json or url (url is returned as a data: URL).")
    model = body.get("model")
    return {
        "prompt": prompt,
        "n": n,
        "size": size,
        "quality": quality,
        "background": background,
        "output_format": output_format,
        "response_format": response_format,
        "model": model if isinstance(model, str) and model.strip() else None,
    }


def nous_images_enabled() -> bool:
    return (environ_get(NOUS_ENABLE_ENV) or "").strip().lower() in _ON


def nous_daily_cap() -> int:
    raw = (environ_get(NOUS_CAP_ENV) or "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_NOUS_CAP
    except ValueError:
        return DEFAULT_NOUS_CAP


def chatgpt_ready() -> bool:
    return chatgpt_auth.login_status()[0] == "ok"


def images_backend(active: str, model: str | None = None) -> str:
    """chatgpt or nous, or raise 503 with what to do."""
    forced = (environ_get(IMAGES_BACKEND_ENV) or "").strip().lower()
    if forced in {"off", "none", "0"}:
        raise GatewayError(
            503,
            "Image generation is off (NEXUS_IMAGES_BACKEND=off).",
            "api_error",
            "images_disabled",
        )
    wants_nous = forced == "nous" or (
        not forced and model is not None and "/" in model and nous_images_enabled()
    )
    if wants_nous or (not forced and active == "nous" and nous_images_enabled()):
        if not nous_images_enabled():
            raise GatewayError(
                403,
                f"Nous images are off by default because Nous bills per image. "
                f"Set {NOUS_ENABLE_ENV}=1 to opt in.",
                "permission_error",
                "nous_images_disabled",
            )
        return "nous"
    if forced not in {"", "chatgpt"}:
        raise GatewayError(
            503,
            f"{IMAGES_BACKEND_ENV} must be chatgpt, nous or off.",
            "api_error",
            "images_backend_unknown",
        )
    if chatgpt_ready():
        return "chatgpt"
    if not forced and nous_images_enabled():
        return "nous"
    raise GatewayError(
        503,
        "No image backend is ready. Sign in once with `codex login` (ChatGPT plan), "
        f"or opt in to Nous images with {NOUS_ENABLE_ENV}=1.",
        "api_error",
        "images_unavailable",
        extra={"hint": chatgpt_auth.login_status()[1]},
    )


def image_model_rows(now: int | None = None) -> list[dict[str, Any]]:
    """Rows /v1/models adds so a client's image-model check finds an id."""
    stamp = int(now if now is not None else time.time())
    rows = []
    if chatgpt_ready() and (environ_get(IMAGES_BACKEND_ENV) or "").strip().lower() in {
        "",
        "chatgpt",
    }:
        rows.append(
            {
                "id": CHATGPT_IMAGE_MODEL,
                "object": "model",
                "created": stamp,
                "owned_by": "openai",
                "kind": "image",
            }
        )
    if nous_images_enabled():
        rows.append(
            {
                "id": nous_model(None),
                "object": "model",
                "created": stamp,
                "owned_by": "nous",
                "kind": "image",
            }
        )
    return rows


def nous_model(requested: str | None) -> str:
    if requested and "/" in requested:
        return requested
    return (environ_get("NEXUS_NOUS_IMAGE_MODEL") or "").strip() or NOUS_IMAGE_MODEL


def _chatgpt_headers(creds: chatgpt_auth.Credentials, turn: str) -> dict[str, str]:
    from nexus.backends.chatgpt import _headers

    headers = _headers(creds, stream=False)
    headers["content-type"] = "application/json"
    headers["x-codex-image-turn-id"] = turn
    return headers


def _unsupported(status: int, text: str) -> bool:
    lowered = text.lower()
    return status in {404, 405} or (
        status == 400
        and (
            "unsupported" in lowered
            or "not supported" in lowered
            or "unknown" in lowered
            and "model" in lowered
        )
    )


class _Fallback(Exception):
    pass


async def _chatgpt_images(req: dict[str, Any], transport: Any) -> list[dict[str, Any]]:
    from nexus.backends.chatgpt import _http_error, _note_usage, chatgpt_base

    body: dict[str, Any] = {
        "prompt": req["prompt"],
        "model": req["model"]
        if (req["model"] or "").startswith("gpt-image")
        else CHATGPT_IMAGE_MODEL,
        "n": req["n"],
        "quality": req["quality"],
        "size": req["size"],
        "background": req["background"],
    }
    if req["output_format"] != "png":
        body["output_format"] = req["output_format"]
    turn = str(uuid.uuid4())
    for attempt in (0, 1):
        creds = await chatgpt_auth.access(force_refresh=attempt == 1)
        try:
            async with httpx.AsyncClient(timeout=upstream_timeout(), transport=transport) as c:
                response = await c.post(
                    f"{chatgpt_base()}/images/generations",
                    headers=_chatgpt_headers(creds, turn),
                    json=body,
                )
        except httpx.TimeoutException:
            raise GatewayError(
                504, "ChatGPT image request timed out", "upstream_error", "chatgpt_timeout"
            ) from None
        except httpx.HTTPError:
            raise GatewayError(
                502, "ChatGPT unreachable", "upstream_error", "chatgpt_unreachable"
            ) from None
        _note_usage(response.headers)
        if response.status_code == 401 and attempt == 0:
            continue
        if response.status_code >= 300:
            text = redact(response.text, [creds.access_token, creds.account_id])
            if _unsupported(response.status_code, text):
                raise _Fallback()
            raise _http_error(response, response.content, creds)
        try:
            data = response.json().get("data") or []
        except ValueError:
            data = []
        rows = [
            {
                "b64_json": d["b64_json"],
                **({"revised_prompt": d["revised_prompt"]} if d.get("revised_prompt") else {}),
            }
            for d in data
            if isinstance(d, dict) and isinstance(d.get("b64_json"), str)
        ]
        if not rows:
            raise GatewayError(
                502, "ChatGPT returned no image", "upstream_error", "chatgpt_no_image"
            )
        return rows
    raise AssertionError("unreachable")  # pragma: no cover


async def _chatgpt_responses(req: dict[str, Any], transport: Any) -> list[dict[str, Any]]:
    """Fallback: /responses with the hosted image_generation tool, one image per call."""
    from nexus.backends.chatgpt import _failed, _http_error, _note_usage, chatgpt_base
    from nexus.backends.chatgpt import _headers as stream_headers
    from nexus.config import model_for

    tool: dict[str, Any] = {
        "type": "image_generation",
        "quality": req["quality"],
        "size": req["size"],
        "output_format": req["output_format"],
        "background": req["background"],
    }
    model = (environ_get(_RESPONSES_MODEL_ENV) or "").strip() or model_for("chatgpt")
    rows: list[dict[str, Any]] = []
    for _ in range(req["n"]):
        body = {
            "model": model,
            "instructions": "Generate the image the user describes.",
            "store": False,
            "stream": True,
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": req["prompt"]}],
                }
            ],
        }
        creds = await chatgpt_auth.access()
        image: dict[str, Any] | None = None
        try:
            async with httpx.AsyncClient(timeout=upstream_timeout(), transport=transport) as c:
                async with c.stream(
                    "POST",
                    f"{chatgpt_base()}/responses",
                    headers=stream_headers(creds, stream=True),
                    json=body,
                ) as r:
                    _note_usage(r.headers)
                    if r.status_code >= 300:
                        raise _http_error(r, await r.aread(), creds)
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:])
                        except ValueError:
                            continue
                        kind = event.get("type")
                        if kind in {"response.failed", "error"}:
                            raise _failed(event)
                        item = event.get("item") or {}
                        if item.get("type") == "image_generation_call" and item.get("result"):
                            image = {"b64_json": item["result"]}
                            if item.get("revised_prompt"):
                                image["revised_prompt"] = item["revised_prompt"]
        except httpx.TimeoutException:
            raise GatewayError(
                504, "ChatGPT image request timed out", "upstream_error", "chatgpt_timeout"
            ) from None
        except httpx.HTTPError:
            raise GatewayError(
                502, "ChatGPT unreachable", "upstream_error", "chatgpt_unreachable"
            ) from None
        if image is None:
            raise GatewayError(
                502, "ChatGPT returned no image", "upstream_error", "chatgpt_no_image"
            )
        rows.append(image)
    return rows


_DATA_URL = re.compile(r"^data:image/[a-z+]+;base64,(.+)$", re.S)


def _nous_extract(body: dict[str, Any]) -> str | None:
    for choice in body.get("choices") or []:
        message = (choice or {}).get("message") or {}
        candidates = list(message.get("images") or [])
        content = message.get("content")
        if isinstance(content, list):
            candidates += [p for p in content if isinstance(p, dict)]
        for part in candidates:
            url = (
                (part.get("image_url") or {}).get("url")
                if isinstance(part.get("image_url"), dict)
                else part.get("image_url") or part.get("url")
            )
            if isinstance(url, str):
                match = _DATA_URL.match(url)
                if match:
                    return match.group(1)
            if isinstance(part.get("b64_json"), str):
                return part["b64_json"]
    return None


async def _nous_images(req: dict[str, Any], transport: Any) -> list[dict[str, Any]]:
    from nexus.backends.nous import load_nous_key
    from nexus.config import nous_base
    from nexus.usage import store

    cap = nous_daily_cap()
    used = store().images_since("nous", _DAY_S)
    if used + req["n"] > cap:
        raise GatewayError(
            429,
            f"Nous image cap reached ({used} of {cap} in the last 24 h). "
            f"Raise {NOUS_CAP_ENV} or wait.",
            "rate_limit_error",
            "nous_image_cap",
        )
    key = load_nous_key()
    if not key:
        raise GatewayError(503, "Nous key is not configured.", "api_error", "nous_key_missing")
    rows: list[dict[str, Any]] = []
    for _ in range(req["n"]):
        body = {
            "model": nous_model(req["model"]),
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": req["prompt"]}],
        }
        try:
            async with httpx.AsyncClient(timeout=upstream_timeout(), transport=transport) as c:
                r = await c.post(
                    f"{nous_base()}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPError:
            raise GatewayError(
                502, "Nous unreachable", "upstream_error", "nous_unreachable"
            ) from None
        if r.status_code == 429:
            raise GatewayError(
                429, "Nous rate or usage limit reached.", "rate_limit_error", "nous_quota"
            )
        if r.status_code >= 300:
            raise GatewayError(
                502,
                f"Nous image request failed (HTTP {r.status_code})",
                "upstream_error",
                "nous_upstream_error",
                extra={"upstream_body": redact(r.text, [key])[:2000]},
            )
        try:
            image = _nous_extract(r.json())
        except ValueError:
            image = None
        if image is None:
            raise GatewayError(502, "Nous returned no image", "upstream_error", "nous_no_image")
        rows.append({"b64_json": image})
    return rows


def _shape(rows: list[dict[str, Any]], req: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        try:
            base64.b64decode(row["b64_json"], validate=True)
        except (binascii.Error, ValueError):
            raise GatewayError(
                502, "Upstream image was not valid base64", "upstream_error", "image_invalid"
            ) from None
        if req["response_format"] == "url":
            mime = "jpeg" if req["output_format"] == "jpeg" else req["output_format"]
            item = {"url": f"data:image/{mime};base64,{row['b64_json']}"}
            if "revised_prompt" in row:
                item["revised_prompt"] = row["revised_prompt"]
            out.append(item)
        else:
            out.append(row)
    return out


async def generate(
    raw: bytes, *, active: str, transport: Any = None, force_route: str | None = None
) -> tuple[dict[str, Any], str, str]:
    """Returns (body, backend id, route used).

    ``force_route="responses"`` skips the plan Images endpoint (diagnostics: the
    ``X-Nexus-Image-Route: responses`` request header, or NEXUS_IMAGES_FORCE_RESPONSES=1).
    """
    if (environ_get("NEXUS_IMAGES_FORCE_RESPONSES") or "").strip().lower() in _ON:
        force_route = "responses"
    req = parse_request(raw)
    backend = images_backend(active, req["model"])
    if backend == "nous":
        rows, route = await _nous_images(req, transport), "nous-chat"
    else:
        try:
            if force_route == "responses":
                raise _Fallback()
            rows, route = await _chatgpt_images(req, transport), "images"
        except _Fallback:
            rows, route = await _chatgpt_responses(req, transport), "responses"
    body = {"created": int(time.time()), "data": _shape(rows, req)}
    try:
        from nexus.usage import store

        store().record_image(backend, len(rows))
    except Exception:
        pass
    return body, backend, route
