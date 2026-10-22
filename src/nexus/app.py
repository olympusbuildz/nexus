"""ASGI app: /health, /doctor, dashboard, OpenAI /v1, and Anthropic POST /v1/messages."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from nexus import __version__
from nexus.anthropic_messages import (
    AnthropicSseTranslator,
    anthropic_error_from_gateway,
    message_to_sse,
    parse_completion_body,
    prepare_native_request,
    to_openai_chat,
)
from nexus.auth import ensure_bridge_key, read_bridge_key, require_bearer
from nexus.backends.anthropic import AnthropicBackend
from nexus.backends.base import Backend
from nexus.backends.registry import active_backend_name, build_backend
from nexus.bench import parse_request, run_bench
from nexus.config import (
    bridge_key_path,
    configured_port,
    loopback_host,
    max_inflight,
)
from nexus.dashboard import DASHBOARD_HTML
from nexus.dashboard_config import config_payload
from nexus.dashboard_models import models_payload
from nexus.dashboard_rabbit import add_rabbit_routes
from nexus.errors import (
    DashboardError,
    GatewayError,
    dashboard_error,
    fallback_hint,
)
from nexus.failover import FailoverState, fallback_config, with_fallback
from nexus.guard import LoopbackGuard
from nexus.images import CHATGPT_IMAGE_MODEL, image_model_rows
from nexus.images import generate as generate_images
from nexus.model_fallback import choose_model, remap_headers
from nexus.models_meta import first_warning
from nexus.nous_token import (
    NousTokenError,
    ensure_nous_token,
    needs_hermes_sync,
    nous_token_refresh_loop,
    sync_nous_token,
)
from nexus.pulse import (
    PulseLog,
    PulseMiddleware,
    note_backend,
    note_error,
    note_model,
    note_tokens,
    note_usage_limit,
)
from nexus.rabbit_watchdog import WATCHDOG, rabbit_watchdog_loop
from nexus.redact import known_secrets, redact, redact_bytes
from nexus.roles import apply_role_model
from nexus.service import dashboard_restart_advice
from nexus.setup_flow import SetupError, apply_dashboard_choice
from nexus.status import doctor_payload, health_payload
from nexus.tool_repair import open_chat_repaired
from nexus.ui_static import UiStaticMiddleware
from nexus.usage_sniff import SseUsage, client_wants_usage

logger = logging.getLogger("nexus")

_DASHBOARD_FONTS = Path(__file__).resolve().parent / "static" / "fonts"
_DASHBOARD_FONT_NAMES = frozenset(
    {
        "syne.woff2",
        "plex-sans.woff2",
        "plex-mono-400.woff2",
        "plex-mono-500.woff2",
    }
)

LOG_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "bridge": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        },
        "access": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "bridge",
            "stream": "ext://sys.stdout",
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "access",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "nexus": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

_V1_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def startup_lines(host: str, port: int) -> list[str]:
    backend = build_backend()
    status = backend.describe()
    summary = {
        "ready": status.get("ready"),
        "key_present": status.get("key_present"),
        "key_source": status.get("key_source"),
        "base": status.get("base"),
        "default_model": status.get("default_model"),
    }
    if "cli_present" in status:
        summary["cli_present"] = status.get("cli_present")
        summary["login"] = status.get("login")
    return [
        f"nexus {__version__} http://{host}:{port}/v1",
        f"bridge key file: {bridge_key_path()}",
        f"active backend: {active_backend_name()}",
        f"{backend.id}: " + json.dumps(summary, sort_keys=True),
    ]


def _restart_fields() -> dict[str, Any]:
    advice = dashboard_restart_advice()
    return {
        "restart_available": advice["available"] == "yes",
        "restart_managed": advice["managed"] == "yes",
        "restart_command": advice["command"],
        "restart_note": advice["note"],
    }


def _dashboard_error(status: int, error: str, code: str, *, no_store: bool = False) -> JSONResponse:
    return JSONResponse(
        dashboard_error(error, code),
        status_code=status,
        headers={"Cache-Control": "no-store"} if no_store else None,
    )


def _ensure_nous_refresh(app: FastAPI) -> None:
    """Start the Hermes token loop if the active backend needs it and it is not running.

    The loop is started at boot when Nous is already the choice. A Settings save
    that switches *to* Nous must start it too, or the copied token would expire
    after 60 minutes with no refresh.
    """
    if not needs_hermes_sync(active_backend_name()):
        return
    existing = getattr(app.state, "nous_refresh_task", None)
    if existing is not None and not existing.done():
        return
    app.state.nous_refresh_task = asyncio.create_task(
        nous_token_refresh_loop(),
        name="nous-token-refresh",
    )


def _resolve(app: FastAPI) -> Backend:
    override = getattr(app.state, "backend", None)
    if override is not None:
        return override
    return build_backend()


def _resolve_chat(app: FastAPI, active: Backend | None = None) -> Backend:
    """The active backend, wrapped in the fallback chain only when one is configured.

    Pass ``active`` to wrap a backend the caller already resolved, so one request
    never sees two different primaries.
    """
    factory = app.state.backend_factory or build_backend
    return with_fallback(_resolve(app) if active is None else active, app.state.failover, factory)


def _failover_headers(backend: Backend) -> dict[str, str]:
    """``X-OS3-Backend`` only when a backend other than the active one answered."""
    served = getattr(backend, "served_by", None)
    return {"X-OS3-Backend": served} if isinstance(served, str) else {}


def _backend_id(request: Request) -> str:
    """Active backend id for a log line, or "unknown". Never a credential."""
    try:
        return _resolve(request.app).id
    except Exception:
        return "unknown"


def _semaphore(app: FastAPI) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = max_inflight()
    current = getattr(app.state, "semaphore", None)
    bound_loop = getattr(app.state, "semaphore_loop", None)
    bound_limit = getattr(app.state, "sem_limit", None)
    if current is None or bound_loop is not loop or bound_limit != limit:
        current = asyncio.Semaphore(limit)
        app.state.semaphore = current
        app.state.semaphore_loop = loop
        app.state.sem_limit = limit
    return current


def _anthropic_client(path: str) -> bool:
    return path.rstrip("/") == "/v1/messages"


def _error_response(exc: GatewayError, *, anthropic: bool) -> Response:
    if anthropic:
        status, payload = anthropic_error_from_gateway(exc)
        raw = json.dumps(payload, ensure_ascii=False)
    else:
        status = exc.status_code
        raw = json.dumps(exc.as_dict(), ensure_ascii=False)
    safe = redact(raw, known_secrets())
    headers: dict[str, str] = {}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return Response(
        content=safe,
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


def _read_chat_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError(
            400,
            "Invalid JSON body",
            "invalid_request_error",
            "invalid_json",
        ) from None
    if not isinstance(payload, dict):
        raise GatewayError(
            400,
            "JSON body must be an object",
            "invalid_request_error",
            "invalid_json",
        )
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise GatewayError(
            400,
            "messages must be a list of objects",
            "invalid_request_error",
            "invalid_messages",
        )
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        from nexus.config import default_model

        payload["model"] = default_model()
    note_model(payload.get("model"))
    stream = payload.get("stream", False)
    if stream is None:
        stream = False
    if not isinstance(stream, bool):
        raise GatewayError(
            400,
            "stream must be a boolean",
            "invalid_request_error",
            "invalid_stream",
        )
    payload["stream"] = stream
    return payload


def create_app(backend: Backend | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _app.state.nous_refresh_task = None
        _ensure_nous_refresh(_app)
        watchdog: asyncio.Task[None] | None = None
        if WATCHDOG.enabled:
            watchdog = asyncio.create_task(rabbit_watchdog_loop(), name="rabbit-watchdog")
        try:
            yield
        finally:
            for running in (getattr(_app.state, "nous_refresh_task", None), watchdog):
                if running is not None:
                    running.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await running

    app = FastAPI(title="nexus", version=__version__, lifespan=lifespan)
    app.add_middleware(UiStaticMiddleware)
    app.add_middleware(LoopbackGuard)
    app.state.backend = backend
    app.state.nous_refresh_task = None
    app.state.semaphore = None
    app.state.semaphore_loop = None
    app.state.sem_limit = None
    app.state.bench_running = False
    app.state.pulse = PulseLog()
    app.state.failover = FailoverState()
    app.state.model_catalog = {}
    app.state.backend_factory = None
    app.state.images_transport = None
    app.add_middleware(PulseMiddleware, pulse=app.state.pulse)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> Response:
        if (
            exc.status_code == 429
            or exc.err_type == "rate_limit_error"
            or str(exc.code).endswith("_usage_limit")
            or "usage_limit" in str(exc.code)
        ):
            note_usage_limit()
        note_error(exc.code, exc.as_dict()["error"]["hint"])
        return _error_response(exc, anthropic=_anthropic_client(request.url.path))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled %s backend=%s path=%s",
            type(exc).__name__,
            _backend_id(request),
            request.url.path,
        )
        note_error("internal_error", fallback_hint(500, "internal_error"))
        path = request.url.path
        if not (path == "/v1" or path.startswith("/v1/")):
            return JSONResponse(
                status_code=500,
                content=dashboard_error(
                    "Internal error", "internal_error", fallback_hint(500, "internal_error")
                ),
            )
        if _anthropic_client(request.url.path):
            content: dict[str, Any] = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Internal error",
                    "hint": fallback_hint(500, "internal_error"),
                },
            }
        else:
            content = {
                "error": {
                    "message": "Internal error",
                    "type": "internal_error",
                    "code": "internal_error",
                    "hint": fallback_hint(500, "internal_error"),
                }
            }
        return JSONResponse(status_code=500, content=content)

    @app.exception_handler(DashboardError)
    async def handle_dashboard_error(_request: Request, exc: DashboardError) -> JSONResponse:
        return JSONResponse(
            exc.as_dict(), status_code=exc.status_code, headers={"Cache-Control": "no-store"}
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/dashboard/fonts/{name}")
    def dashboard_font(name: str) -> Response:
        if name not in _DASHBOARD_FONT_NAMES:
            return Response(status_code=404)
        path = _DASHBOARD_FONTS / name
        if not path.is_file():
            return Response(status_code=404)
        return Response(
            path.read_bytes(),
            media_type="font/woff2",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/dashboard/config")
    def dashboard_config_read() -> JSONResponse:
        return JSONResponse(
            {**config_payload(), **_restart_fields()}, headers={"Cache-Control": "no-store"}
        )

    @app.post("/dashboard/config")
    async def dashboard_config(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return _dashboard_error(400, "Expected a JSON object.", "invalid_json")
        backend = payload.get("backend")
        api_key = payload.get("api_key")
        model = payload.get("model")
        activate = payload.get("activate", True)
        if not isinstance(backend, str) or not backend.strip():
            return _dashboard_error(400, "Choose a backend.", "invalid_request")
        if api_key is not None and not isinstance(api_key, str):
            return _dashboard_error(400, "Key must be text.", "invalid_request")
        if model is not None and not isinstance(model, str):
            return _dashboard_error(400, "Model must be text.", "invalid_request")
        if not isinstance(activate, bool):
            return _dashboard_error(400, "activate must be true or false.", "invalid_request")
        try:
            result = await asyncio.to_thread(
                apply_dashboard_choice,
                backend=backend,
                api_key=api_key,
                model=model,
                activate=activate,
            )
        except SetupError as exc:
            status = 409 if exc.code == "not_ready" else 400
            return JSONResponse(dashboard_error(str(exc), exc.code, exc.hint), status_code=status)
        if result.get("activated") == "yes" and needs_hermes_sync(result["backend"]):
            synced = await asyncio.to_thread(sync_nous_token)
            if not synced.ok:
                result["warning"] = redact(synced.message)
            _ensure_nous_refresh(request.app)
        return JSONResponse({"ok": True, **result})

    @app.post("/dashboard/bench")
    async def dashboard_bench(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DashboardError(400, "Expected a JSON object.", "invalid_json") from None
        override = getattr(app.state, "backend", None)
        bench = parse_request(payload, active=active_backend_name())
        if app.state.bench_running:
            raise DashboardError(409, "A bench is already running.", "bench_busy")
        app.state.bench_running = True
        try:
            body = await run_bench(
                bench,
                gate=_semaphore(app),
                override=override,
                cancelled=request.is_disconnected,
            )
        finally:
            app.state.bench_running = False
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        payload = await health_payload(
            backend=getattr(app.state, "backend", None),
            pulse=app.state.pulse,
            failover=app.state.failover,
        )
        payload.update(_restart_fields())
        return payload

    @app.get("/doctor")
    async def doctor() -> dict[str, Any]:
        return await doctor_payload(
            backend=getattr(app.state, "backend", None),
            pulse=app.state.pulse,
            failover=app.state.failover,
        )

    @app.get("/dashboard/pulse")
    def dashboard_pulse() -> dict[str, Any]:
        pulse: PulseLog = app.state.pulse
        failover: FailoverState = app.state.failover
        return {
            "cap": pulse.cap,
            "entries": pulse.snapshot(),
            "traffic": {**pulse.traffic(), "failovers": failover.failovers},
            "fallback": failover.snapshot(fallback_config(), active_backend_name()),
        }

    @app.get("/dashboard/usage")
    async def dashboard_usage() -> JSONResponse:
        from nexus.dashboard_usage import usage_payload

        payload = await usage_payload(active=active_backend_name(), failover=app.state.failover)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/dashboard/models")
    async def dashboard_models(backend: str | None = None) -> JSONResponse:
        name = backend.strip() if backend and backend.strip() else active_backend_name()
        payload = await models_payload(name)
        if payload is None:
            return _dashboard_error(
                404, f"Unknown backend '{name}'.", "unknown_backend", no_store=True
            )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/dashboard/bridge-key")
    def dashboard_bridge_key() -> JSONResponse:
        key = read_bridge_key()
        headers = {"Cache-Control": "no-store"}
        if not key:
            return JSONResponse({"present": False}, headers=headers)
        return JSONResponse({"present": True, "key": key}, headers=headers)

    add_rabbit_routes(app, _resolve)

    @app.get("/v1/models", dependencies=[Depends(require_bearer)])
    async def list_models() -> dict[str, Any]:
        backend = _resolve(app)
        note_backend(backend.id)
        rows = await backend.list_models()
        body: dict[str, Any] = {"object": "list", "data": rows}
        warning = first_warning(rows)
        if warning is not None:
            body["warning"] = warning
        known = {r.get("id") for r in rows}
        body["data"] = rows + [r for r in image_model_rows() if r["id"] not in known]
        return body

    @app.post("/v1/images/generations", dependencies=[Depends(require_bearer)])
    async def images_generations(request: Request) -> Response:
        raw = await request.body()
        active = _resolve(app).id
        body, served, route = await generate_images(
            raw,
            active=active,
            transport=app.state.images_transport,
            force_route=(request.headers.get("x-nexus-image-route") or "").strip().lower() or None,
        )
        note_backend(served)
        note_model(CHATGPT_IMAGE_MODEL if served == "chatgpt" else "image")
        return JSONResponse(body, headers={"X-OS3-Backend": served, "X-Nexus-Image-Route": route})

    @app.post("/v1/images/edits", dependencies=[Depends(require_bearer)])
    async def images_edits() -> Response:
        raise GatewayError(
            501,
            "Image edits are not supported by Nexus yet. Use /v1/images/generations.",
            "invalid_request_error",
            "images_edits_unsupported",
        )

    @app.post("/v1/images/variations", dependencies=[Depends(require_bearer)])
    async def images_variations() -> Response:
        raise GatewayError(
            501,
            "Image variations are not supported. Use /v1/images/generations.",
            "invalid_request_error",
            "images_variations_unsupported",
        )

    @app.post("/v1/chat/completions", dependencies=[Depends(require_bearer)])
    async def chat_completions(request: Request) -> Response:
        payload = _read_chat_payload(await request.body())
        before_role = payload["model"]
        apply_role_model(payload)
        headers: dict[str, str] = {}
        backend = _resolve(app)
        if payload["model"] == before_role:
            choice = await choose_model(backend, before_role, app.state.model_catalog)
            payload["model"] = choice.model
            headers = remap_headers(choice)
            if headers:
                app.state.pulse.note_model_remapped()
        note_model(payload["model"])
        return await _dispatch_chat(app, backend, payload, headers)

    @app.post("/v1/messages", dependencies=[Depends(require_bearer)])
    async def messages(request: Request) -> Response:
        backend = _resolve(app)
        payload = _parse_object(await request.body())
        raw_model = payload.get("model")
        if isinstance(raw_model, str):
            note_model(raw_model)
        if isinstance(backend, AnthropicBackend):
            prepared = prepare_native_request(payload)
            note_model(prepared.get("model"))
            return await _dispatch_native_messages(app, backend, prepared)
        chat = to_openai_chat(payload, prefix_claude=backend.id == "openrouter")
        note_model(chat.get("model"))
        return await _dispatch_messages(app, chat)

    @app.api_route(
        "/v1",
        methods=_V1_METHODS,
        dependencies=[Depends(require_bearer)],
        include_in_schema=False,
    )
    async def v1_root() -> None:
        raise GatewayError(
            404,
            "Unknown endpoint: /v1",
            "invalid_request_error",
            "unknown_endpoint",
        )

    @app.api_route(
        "/v1/{rest:path}",
        methods=_V1_METHODS,
        dependencies=[Depends(require_bearer)],
        include_in_schema=False,
    )
    async def v1_unknown(rest: str) -> None:
        raise GatewayError(
            404,
            f"Unknown endpoint: /v1/{rest}",
            "invalid_request_error",
            "unknown_endpoint",
        )

    return app


async def _dispatch_chat(
    app: FastAPI,
    active: Backend,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> Response:
    sem = _semaphore(app)
    await sem.acquire()
    release_now = True
    try:
        backend = _resolve_chat(app, active)
        note_backend(backend.id)
        upstream = await open_chat_repaired(backend, payload)
        if upstream.stream is None:
            body = upstream.body or b""
            _note_usage_from_body(body)
            return Response(
                content=body,
                status_code=upstream.status_code,
                media_type=upstream.media_type,
                headers={**(extra_headers or {}), **_failover_headers(backend)},
            )
        release_now = False
        agen = upstream.stream

        sniff = SseUsage(drop_usage_only=not client_wants_usage(payload))

        async def guarded() -> AsyncIterator[bytes]:
            try:
                async for chunk in _terminated(agen):
                    out = sniff.feed(chunk)
                    if out:
                        yield out
                rest = sniff.finish()
                if rest:
                    yield rest
            finally:
                sem.release()

        return StreamingResponse(
            guarded(),
            status_code=upstream.status_code,
            media_type=upstream.media_type or "text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **(extra_headers or {}),
                **_failover_headers(backend),
            },
        )
    finally:
        if release_now:
            sem.release()


async def _terminated(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Pass an OpenAI SSE stream through, ending it with exactly one `data: [DONE]`.

    Some upstreams (Nous sends `"lastOne":true`) just stop. A client that waits
    for [DONE] would hang. If the stream raises, nothing is appended.
    """
    tail = b""
    async for chunk in stream:
        yield chunk
        tail = (tail + chunk)[-64:]
    text = tail.rstrip()
    if text.endswith(b"data: [DONE]") or text.endswith(b"data:[DONE]"):
        return
    if not tail or tail.endswith((b"\n\n", b"\r\n\r\n")):
        gap = b""
    else:
        gap = b"\n" if tail.endswith(b"\n") else b"\n\n"
    yield gap + b"data: [DONE]\n\n"


def _note_usage_from_body(raw: bytes) -> None:
    """Record token counts only when the upstream body already carries them."""
    try:
        payload = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        note_tokens(prompt=usage.get("prompt_tokens"), completion=usage.get("completion_tokens"))
        return
    if "input_tokens" in usage or "output_tokens" in usage:
        note_tokens(prompt=usage.get("input_tokens"), completion=usage.get("output_tokens"))


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError(
            400,
            "Invalid JSON body",
            "invalid_request_error",
            "invalid_json",
        ) from None
    if not isinstance(payload, dict):
        raise GatewayError(
            400,
            "JSON body must be an object",
            "invalid_request_error",
            "invalid_json",
        )
    return payload


async def _dispatch_native_messages(
    app: FastAPI,
    backend: AnthropicBackend,
    payload: dict[str, Any],
) -> Response:
    """Forward one Messages request to a backend that speaks Messages natively."""
    sem = _semaphore(app)
    await sem.acquire()
    release_now = True
    try:
        note_backend(backend.id)
        upstream = await backend.open_messages(payload, stream=bool(payload["stream"]))
        secrets = known_secrets()
        if upstream.stream is None:
            raw = upstream.body or b""
            media = upstream.media_type or "application/json"
            if payload["stream"]:
                message = _message_or_none(raw)
                if message is not None:
                    _note_usage_from_body(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                    raw = message_to_sse(message)
                    media = "text/event-stream"
            else:
                _note_usage_from_body(raw)
            return Response(
                content=redact_bytes(raw, secrets),
                status_code=upstream.status_code,
                media_type=media,
                headers={"Cache-Control": "no-cache"} if media == "text/event-stream" else {},
            )
        release_now = False
        agen = upstream.stream

        sniff = SseUsage()

        async def guarded() -> AsyncIterator[bytes]:
            try:
                async for chunk in agen:
                    out = sniff.feed(chunk)
                    if out:
                        yield redact_bytes(out, secrets)
                rest = sniff.finish()
                if rest:
                    yield redact_bytes(rest, secrets)
            finally:
                sem.release()

        return StreamingResponse(
            guarded(),
            status_code=upstream.status_code,
            media_type=upstream.media_type or "text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    finally:
        if release_now:
            sem.release()


def _message_or_none(raw: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("type") == "message":
        return payload
    return None


async def _dispatch_messages(app: FastAPI, payload: dict[str, Any]) -> Response:
    sem = _semaphore(app)
    await sem.acquire()
    release_now = True
    try:
        backend = _resolve_chat(app)
        note_backend(backend.id)
        upstream = await open_chat_repaired(backend, payload)
        if upstream.stream is None:
            message = parse_completion_body(upstream.body or b"", fallback_model=payload["model"])
            _note_usage_from_body(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            if payload["stream"]:
                raw = message_to_sse(message)
                media = "text/event-stream"
            else:
                raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
                media = "application/json"
            return Response(
                content=redact_bytes(raw, known_secrets()),
                status_code=200,
                media_type=media,
                headers={
                    **({"Cache-Control": "no-cache"} if payload["stream"] else {}),
                    **_failover_headers(backend),
                },
            )
        release_now = False
        translator = AnthropicSseTranslator(payload["model"])
        agen = upstream.stream
        secrets = known_secrets()

        sniff = SseUsage()

        async def guarded() -> AsyncIterator[bytes]:
            try:
                async for chunk in agen:
                    for event in translator.feed(chunk):
                        sniff.feed(event)
                        yield redact_bytes(event, secrets)
                for event in translator.finish():
                    sniff.feed(event)
                    yield redact_bytes(event, secrets)
                sniff.finish()
            except Exception as exc:
                logger.error("messages stream failed: %s", type(exc).__name__)
                if not translator.closed:
                    yield redact_bytes(translator.fail("Internal error"), secrets)
            finally:
                sem.release()

        return StreamingResponse(
            guarded(),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **_failover_headers(backend),
            },
        )
    finally:
        if release_now:
            sem.release()


app = create_app()


def _enforce_default_model() -> None:
    """Refuse to open the door on a model no configured backend can serve.

    Runs before uvicorn binds, so a stranger with an unreachable default gets
    the reason at startup rather than a 404 on their first real request. A
    backend that could not be checked is not treated as a failure.
    """
    from nexus.startup import DefaultModelUnavailable, enforce_default_model

    try:
        checks = asyncio.run(enforce_default_model())
    except DefaultModelUnavailable as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    unverified = [c for c in checks if c.reachable is not True]
    if unverified:
        for check in unverified:
            logger.warning(
                "default model %s unverified on %s: %s",
                check.model,
                check.backend,
                check.detail or "unknown",
            )


def _ensure_nous_token() -> None:
    """Refuse to bind when the nous backend has no live Hermes token.

    ``./nexus start`` syncs before installing the unit; this covers
    ``./nexus run`` and LaunchAgent/systemd restarts that skip start.
    """
    if not needs_hermes_sync(active_backend_name()):
        return
    try:
        ensure_nous_token()
    except NousTokenError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1) from None


def main() -> None:
    host = loopback_host()
    port = configured_port()
    ensure_bridge_key()
    _ensure_nous_token()
    _enforce_default_model()
    for line in startup_lines(host, port):
        print(line, flush=True)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=LOG_CONFIG,
        access_log=True,
        proxy_headers=False,
        forwarded_allow_ips="",
    )
