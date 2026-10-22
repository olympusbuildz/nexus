"""``./nexus test``: print what to paste into a client, then prove it works.

One real round trip against the running local door: reach it, authenticate, chat,
and (where the backend can) call a tool and feed the result back. The bridge key
is sent as a header and is never printed; only its file path is.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from nexus.auth import read_bridge_key
from nexus.config import (
    ANTIGRAVITY_ENABLE_ENV,
    active_backend_id,
    bridge_key_path,
    configured_port,
    model_for,
)
from nexus.redact import known_secrets, redact
from nexus.setup_flow import SUBSCRIPTION

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"

HEALTH_TIMEOUT = httpx.Timeout(5.0)
MODELS_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
CHAT_TIMEOUT = httpx.Timeout(60.0, connect=3.0)

ALT_HOST = "127.1"
RESTART = (
    "restart it (./nexus stop && ./nexus start, "
    "or ./nexus stop && ./nexus run if you run it in a terminal)"
)
TOOL_NAME = "nexus_add"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Add two integers and return the sum.",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    }
]
TOOL_PROMPT = f"Use the {TOOL_NAME} tool to add 2 and 3. Call the tool; do not answer from memory."


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def tool_arguments_error(raw: Any) -> str | None:
    """Why ``raw`` is not a valid ``nexus_add`` argument object, or None when it is."""
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "the tool arguments were not valid JSON"
    if not isinstance(args, dict):
        return "the tool arguments were not a JSON object"
    if set(args) != {"a", "b"}:
        return f"the tool arguments had keys {sorted(args)}, expected ['a', 'b']"
    for key in ("a", "b"):
        if not isinstance(args[key], int) or isinstance(args[key], bool):
            return f"argument '{key}' was not an integer"
    return None


def _message(body: Any) -> dict[str, Any]:
    choices = body.get("choices") if isinstance(body, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return message
    return {}


def _error_parts(body: Any) -> tuple[str, str, str]:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return (
            str(error.get("message") or ""),
            str(error.get("code") or ""),
            str(error.get("hint") or ""),
        )
    return "", "", ""


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


class SelfTest:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        out: Callable[[str], None] = print,
        wait: float = 0.0,
    ) -> None:
        self.wait = wait
        self.transport = transport
        self.out = out
        self.port = configured_port()
        self.key = read_bridge_key()
        self.secrets = known_secrets() + ([self.key] if self.key else [])
        self.steps: list[Step] = []

    def _client(self, host: str, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport,
            base_url=f"http://{host}:{self.port}",
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def _clean(self, text: str) -> str:
        return redact(" ".join(text.split()), self.secrets)

    def _record(self, name: str, status: str, detail: str = "", fix: str = "") -> Step:
        step = Step(name, status, self._clean(detail), self._clean(fix))
        self.steps.append(step)
        self.out(f"  {status:<4}  {name:<18} {step.detail}".rstrip())
        if step.fix:
            self.out(f"        fix: {step.fix}")
        return step

    def _header(self, health: dict[str, Any] | None) -> None:
        backend = (health or {}).get("backend") or active_backend_id()
        model = (health or {}).get("model") or model_for(str(backend))
        key_path = bridge_key_path()
        key_state = "present" if self.key else "absent"
        self.out("Nexus: test my setup")
        self.out("")
        self.out("Paste into OS3 / your client:")
        self.out(f"  Base URL   http://127.0.0.1:{self.port}/v1")
        self.out(
            f"             if your client returns 403 for 127.0.0.1, try http://{ALT_HOST}:{self.port}/v1"
        )
        self.out(f"  API key    the one line in {key_path} ({key_state}; not printed here)")
        self.out(f"  Backend    {backend}")
        self.out(f"  Model      {model}")
        self.out("")
        self.out("Checks:")

    def _holder_fix(self) -> str:
        from nexus.port_holder import report

        lines = [line.strip() for line in report(self.port)]
        return " | ".join(lines[1:]) if len(lines) > 1 else ""

    async def _health(self) -> tuple[dict[str, Any] | None, Step | None]:
        """The door's own /health, or the failed step that says why there is none."""
        failure: tuple[str, str] | None = None
        deadline = time.monotonic() + self.wait
        try:
            while True:
                try:
                    async with self._client("127.0.0.1", HEALTH_TIMEOUT) as client:
                        response = await client.get("/health")
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.5)
        except httpx.ConnectError:
            failure = (
                f"nothing is listening on 127.0.0.1:{self.port}",
                "Start it: ./nexus start (or ./nexus run in a terminal), then re-run.",
            )
        except httpx.HTTPError as exc:
            failure = (
                f"127.0.0.1:{self.port} did not answer in time ({type(exc).__name__})",
                "",
            )
        else:
            body = _json(response)
            if isinstance(body, dict) and body.get("service") in {"nexus", "os3-bridge"}:
                return body, None
            who = f"127.0.0.1:{self.port}"
            failure = (f"{who} answered, but not as nexus (HTTP {response.status_code})", "")
        detail, fix = failure
        if not fix:
            holder = self._holder_fix()
            fix = holder or (
                f"Find the holder with: lsof -nP -iTCP:{self.port} -sTCP:LISTEN. "
                "Nothing was stopped; close it, then ./nexus start."
            )
        return None, Step("server", FAIL, detail, fix)

    @staticmethod
    def _gated_off(health: dict[str, Any]) -> bool:
        """True when the door reports the active backend as off until the user opts in.

        The door sets ``enabled: False`` on the row from ``antigravity_enabled()``; this
        reads that report instead of asking again, so the server's answer is the one used.
        """
        backend = str(health.get("backend"))
        row = (health.get("backends") or {}).get(backend) or {}
        return backend == "antigravity" and row.get("enabled") is False

    def _skip_gated(self, health: dict[str, Any]) -> None:
        backend = str(health.get("backend"))
        detail = f"{backend} is off until you opt in (higher terms-of-service risk)"
        fix = (
            f"To use it, set {ANTIGRAVITY_ENABLE_ENV}=1 for the door, then "
            f"{RESTART}. Or pick another backend: ./nexus setup."
        )
        self._record("backend", SKIP, detail, fix)

    def _skip_gated_round_trips(self, backend: str) -> None:
        for name in ("chat", "tool call"):
            self._record(
                name, SKIP, f"needs {backend}; opt in with {ANTIGRAVITY_ENABLE_ENV}=1 to run it"
            )

    def _backend_step(self, health: dict[str, Any]) -> Step:
        backend = str(health.get("backend"))
        row = (health.get("backends") or {}).get(backend) or {}
        state = row.get("state")
        hint = str(health.get("model_hint") or row.get("hint") or "")
        if health.get("ready") is not False:
            note = "ready" if health.get("ready") is True else f"not confirmed ({hint})"
            return self._record("backend", PASS, f"{backend}: {note}")
        if state == "not_configured":
            fix = f"No provider key. Run ./nexus setup, then {RESTART}."
        elif state == "cli_missing":
            fix = "Install the official CLI for this backend, then run ./nexus setup."
        elif state == "not_logged_in":
            fix = "Sign in inside the official CLI (./doctor.sh shows which), then re-run."
        else:
            fix = "Run ./doctor.sh for the reason, or ./nexus setup to pick another backend."
        return self._record("backend", FAIL, f"{backend} is not ready: {hint}", fix)

    def _explain(self, response: httpx.Response) -> tuple[str, str]:
        message, code, hint = _error_parts(_json(response))
        status = response.status_code
        detail = f"HTTP {status}" + (f" {code}" if code else "")
        if message:
            detail += f": {message}"
        if status == 401:
            return detail, (
                f"The bridge key was refused. Use the one line in {bridge_key_path()} "
                "(the running service must read the same file)."
            )
        if status == 403:
            return detail, (
                f"Something between here and the door refused the request. "
                f"Try the alternative URL http://{ALT_HOST}:{self.port}/v1."
            )
        if status == 503:
            return detail, hint or "The backend is not ready. Run ./doctor.sh."
        return detail, hint or "Run ./doctor.sh for the full reason."

    async def _models(self, host: str, *, primary: bool) -> Step:
        name = f"/v1 via {host}"
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            async with self._client(host, MODELS_TIMEOUT) as client:
                response = await client.get("/v1/models", headers=headers)
        except httpx.HTTPError as exc:
            detail = f"no answer ({type(exc).__name__})"
            if primary:
                return self._record(name, FAIL, detail, "Run ./doctor.sh.")
            return self._record(name, INFO, detail + "; the primary URL is unaffected")
        if response.status_code == 200:
            detail = "answered 200" if primary else "answered 200 (alternative URL)"
            return self._record(name, PASS if primary else INFO, detail)
        detail, fix = self._explain(response)
        if primary:
            if not self.key:
                fix = f"No bridge key file at {bridge_key_path()}. Run ./nexus setup."
            return self._record(name, FAIL, detail, fix)
        return self._record(name, INFO, detail)

    async def _chat(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.key}"}
        return await client.post("/v1/chat/completions", headers=headers, json=payload)

    async def _plain_chat(self, model: str) -> Step:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            "stream": False,
            "max_tokens": 16,
        }
        try:
            async with self._client("127.0.0.1", CHAT_TIMEOUT) as client:
                response = await self._chat(client, payload)
        except httpx.HTTPError as exc:
            return self._record(
                "chat", FAIL, f"no answer in time ({type(exc).__name__})", "Run ./doctor.sh."
            )
        if response.status_code != 200:
            detail, fix = self._explain(response)
            return self._record("chat", FAIL, detail, fix)
        if not str(_message(_json(response)).get("content") or "").strip():
            return self._record(
                "chat",
                FAIL,
                "HTTP 200 but the reply was empty",
                f"Try another model: set NEXUS_MODEL, then {RESTART}.",
            )
        return self._record("chat", PASS, "got a reply")

    def _tool_fail(self, detail: str) -> Step:
        return self._record(
            "tool call",
            FAIL,
            detail,
            "This model did not do tools. Pick one that supports tool calling "
            f"(set NEXUS_MODEL, then {RESTART}).",
        )

    async def _tool_round_trip(self, model: str, backend: str) -> Step:
        if backend in SUBSCRIPTION:
            return self._record(
                "tool call",
                SKIP,
                f"{backend} is a subscription CLI, and subscription CLIs refuse tools",
            )
        first = {
            "model": model,
            "messages": [{"role": "user", "content": TOOL_PROMPT}],
            "tools": TOOLS,
            "stream": False,
            "max_tokens": 256,
        }
        try:
            async with self._client("127.0.0.1", CHAT_TIMEOUT) as client:
                response = await self._chat(client, first)
                if response.status_code == 400 and _error_parts(_json(response))[1] in {
                    "tools_not_supported"
                }:
                    return self._record("tool call", SKIP, "this backend refuses tools")
                if response.status_code != 200:
                    detail, fix = self._explain(response)
                    if response.status_code in {400, 404, 422, 502}:
                        return self._tool_fail(detail)
                    return self._record("tool call", FAIL, detail, fix)
                message = _message(_json(response))
                calls = message.get("tool_calls")
                if not isinstance(calls, list) or not calls:
                    return self._tool_fail("the model answered in text and called no tool")
                call = calls[0] if isinstance(calls[0], dict) else {}
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                if function.get("name") != TOOL_NAME:
                    return self._tool_fail(
                        f"the model called '{function.get('name')}', not {TOOL_NAME}"
                    )
                problem = tool_arguments_error(function.get("arguments"))
                if problem:
                    return self._tool_fail(problem)
                second = {
                    "model": model,
                    "messages": [
                        first["messages"][0],
                        {"role": "assistant", "content": None, "tool_calls": calls},
                        {"role": "tool", "tool_call_id": call.get("id", ""), "content": "5"},
                    ],
                    "tools": TOOLS,
                    "stream": False,
                    "max_tokens": 256,
                }
                response = await self._chat(client, second)
        except httpx.HTTPError as exc:
            return self._record(
                "tool call", FAIL, f"no answer in time ({type(exc).__name__})", "Run ./doctor.sh."
            )
        if response.status_code != 200:
            detail, fix = self._explain(response)
            return self._record("tool call", FAIL, f"sending the tool result failed: {detail}", fix)
        final = _message(_json(response))
        if not str(final.get("content") or "").strip():
            return self._tool_fail("sent the tool result, but there was no final answer")
        return self._record(
            "tool call", PASS, "valid tool call, result sent, final answer received"
        )

    async def run(self) -> int:
        health, failed = await self._health()
        self._header(health)
        if failed is not None:
            self._record(failed.name, failed.status, failed.detail, failed.fix)
            return self._finish()
        assert health is not None
        self._record(
            "server", PASS, f"nexus {health.get('version') or ''} answered".replace("  ", " ")
        )
        gated = self._gated_off(health)
        if gated:
            self._skip_gated(health)
        elif self._backend_step(health).status == FAIL:
            return self._finish()
        primary = await self._models("127.0.0.1", primary=True)
        await self._models(ALT_HOST, primary=False)
        if primary.status == FAIL:
            return self._finish()
        if gated:
            self._skip_gated_round_trips(str(health.get("backend")))
            return self._finish()
        model = str(health.get("model") or model_for(str(health.get("backend"))))
        if (await self._plain_chat(model)).status == FAIL:
            return self._finish()
        await self._tool_round_trip(model, str(health.get("backend")))
        return self._finish()

    def _finish(self) -> int:
        failed = [step for step in self.steps if step.status == FAIL]
        passed = sum(1 for step in self.steps if step.status == PASS)
        skipped = sum(1 for step in self.steps if step.status == SKIP)
        self.out("")
        if failed:
            self.out(f"RESULT: FAIL ({len(failed)} failed, {passed} passed, {skipped} skipped)")
            return 1
        self.out(f"RESULT: PASS ({passed} passed, {skipped} skipped)")
        return 0


async def run_selftest(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Callable[[str], None] = print,
    wait: float = 0.0,
) -> int:
    return await SelfTest(transport=transport, out=out, wait=wait).run()
