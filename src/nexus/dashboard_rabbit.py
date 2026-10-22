"""``/dashboard/rabbit``: the Rabbit OS3 setup engine over HTTP, for the Settings screen.

Same JSON as ``nexus.rabbit_setup``. These routes sit behind the guard every
non-``/v1`` route already has (loopback Host, same-origin JSON writes), and every write
is refused unless ``NEXUS_RABBIT_SETUP=on``. No response carries the bridge key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nexus.backends.base import Backend
from nexus.errors import DashboardError
from nexus.rabbit_setup import MODELS_TIMEOUT, RabbitError, RabbitSetup

NO_STORE = {"Cache-Control": "no-store"}


def add_rabbit_routes(app: FastAPI, resolve: Callable[[FastAPI], Backend]) -> None:
    """Register the routes. ``resolve`` is how the app finds its active backend."""

    def engine() -> RabbitSetup:
        factory = getattr(app.state, "rabbit_factory", None)
        if factory is not None:
            return factory()

        async def models() -> list[dict[str, Any]]:
            return await asyncio.wait_for(resolve(app).list_models(), timeout=MODELS_TIMEOUT)

        async def pulse() -> list[dict[str, Any]]:
            return app.state.pulse.snapshot()

        return RabbitSetup(models_source=models, pulse_source=pulse)

    def refused(exc: RabbitError) -> JSONResponse:
        return JSONResponse(exc.as_dict(), status_code=exc.status, headers=NO_STORE)

    def answered(result: dict[str, Any]) -> JSONResponse:
        failed = not result["ok"] and result.get("mode") != "manual"
        return JSONResponse(result, status_code=502 if failed else 200, headers=NO_STORE)

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            payload = json.loads((await request.body()).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            raise DashboardError(400, "Expected a JSON object.", "invalid_json")
        return payload

    @app.get("/dashboard/rabbit")
    async def rabbit_read(model: str | None = None) -> JSONResponse:
        rabbit = engine()
        plan = await rabbit.plan(model)
        status = await rabbit.status()
        return JSONResponse(
            {"ok": True, "opt_in": status["opt_in"], "plan": plan, "status": status},
            headers=NO_STORE,
        )

    @app.post("/dashboard/rabbit/apply")
    async def rabbit_apply(request: Request) -> JSONResponse:
        payload = await body_of(request)
        model = payload.get("model")
        dry_run = payload.get("dry_run", False)
        if model is not None and not isinstance(model, str):
            raise DashboardError(400, "model must be text.", "invalid_request")
        if not isinstance(dry_run, bool):
            raise DashboardError(400, "dry_run must be true or false.", "invalid_request")
        accept_no_rollback = payload.get("accept_no_rollback", False)
        if not isinstance(accept_no_rollback, bool):
            raise DashboardError(
                400,
                "accept_no_rollback must be true or false.",
                "invalid_request",
                "Send true or false, or leave it out.",
            )
        return await _exclusive(
            lambda rabbit: rabbit.apply(
                model=model or None, dry_run=dry_run, accept_no_rollback=accept_no_rollback
            )
        )

    @app.post("/dashboard/rabbit/restore")
    async def rabbit_restore(request: Request) -> JSONResponse:
        await body_of(request)
        return await _exclusive(lambda rabbit: rabbit.restore())

    async def _exclusive(work: Callable[[RabbitSetup], Any]) -> JSONResponse:
        if getattr(app.state, "rabbit_running", False):
            raise DashboardError(
                409,
                "A Rabbit OS3 change is already running.",
                "rabbit_busy",
                "Wait for it to finish, then try again.",
            )
        app.state.rabbit_running = True
        try:
            return answered(await work(engine()))
        except RabbitError as exc:
            return refused(exc)
        finally:
            app.state.rabbit_running = False
