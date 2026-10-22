"""Live checks for /health, /v1/models, and /v1/chat/completions.

Prints status flags only. The bridge key is sent as a header and never written
to the report. This module does not bind a port and does not restart a server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from nexus.auth import read_bridge_key
from nexus.config import configured_port, default_model, environ_get
from nexus.redact import known_secrets, redact

_TIMEOUT = httpx.Timeout(45.0)


def _flag(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "none"
    return str(value)


def _line(name: str, passed: bool, **fields: object) -> str:
    parts = [name, "PASS" if passed else "FAIL"]
    for key, value in fields.items():
        if key == "code" and value == "":
            continue
        parts.append(f"{key}={_flag(value)}")
    return " ".join(parts)


def _error_code(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


def _assistant_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


async def execute_probe(client: httpx.AsyncClient, *, bridge_key: str | None) -> tuple[int, str]:
    """Return (exit_code, report). The report is redacted before return."""
    secrets = known_secrets()
    if bridge_key and bridge_key not in secrets:
        secrets.append(bridge_key)
    lines: list[str] = []
    failed = False

    try:
        health = await client.get("/health")
    except httpx.HTTPError:
        text = redact("health FAIL unreachable", secrets)
        return 1, text

    health_body: Any
    try:
        health_body = health.json()
    except ValueError:
        health_body = {}
    catalog = health_body.get("backends") if isinstance(health_body, dict) else None
    backend = health_body.get("backend") if isinstance(health_body, dict) else None
    active = (
        catalog.get(backend) if isinstance(catalog, dict) and isinstance(backend, str) else None
    )
    ready = active.get("ready") if isinstance(active, dict) else None
    openrouter = health_body.get("openrouter") if isinstance(health_body, dict) else None
    openrouter_ready = openrouter.get("ready") if isinstance(openrouter, dict) else None
    codex_required = health_body.get("codex_required") if isinstance(health_body, dict) else None
    ok_flag = health_body.get("ok") if isinstance(health_body, dict) else None
    model = health_body.get("model") if isinstance(health_body, dict) else None
    model_hint = health_body.get("model_hint") if isinstance(health_body, dict) else None
    probe_model = model if isinstance(model, str) and model else default_model()
    health_ok = (
        health.status_code == 200
        and ok_flag is True
        and codex_required is False
        and health_body.get("ready") is True
    )
    lines.append(
        _line(
            "health",
            health_ok,
            status=health.status_code,
            ok=ok_flag,
            backend=backend,
            ready=ready,
            model=model,
            codex_required=codex_required,
            openrouter_ready=openrouter_ready,
        )
    )
    if not health_ok and isinstance(model_hint, str) and model_hint not in {"", "ok"}:
        lines.append(f"model FAIL model={model} hint={model_hint}")
    roles_hint = health_body.get("roles_hint") if isinstance(health_body, dict) else None
    if not health_ok and isinstance(roles_hint, str) and roles_hint:
        lines.append(f"roles FAIL hint={roles_hint}")
    failed = failed or not health_ok

    if not bridge_key:
        lines.append("models FAIL bridge_key_missing")
        lines.append("chat FAIL bridge_key_missing")
        return 1, redact("\n".join(lines), secrets)

    headers = {"Authorization": f"Bearer {bridge_key}"}
    try:
        models = await client.get("/v1/models", headers=headers)
    except httpx.HTTPError:
        lines.append("models FAIL unreachable")
        lines.append("chat FAIL unreachable")
        return 1, redact("\n".join(lines), secrets)

    try:
        models_body = models.json()
    except ValueError:
        models_body = {}
    data = models_body.get("data") if isinstance(models_body, dict) else None
    ids = [row.get("id") for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    listed = [model_id for model_id in ids if isinstance(model_id, str)]
    default_listed = probe_model in listed
    models_ok = (
        models.status_code == 200
        and isinstance(models_body, dict)
        and models_body.get("object") == "list"
        and default_listed
    )
    lines.append(
        _line(
            "models",
            models_ok,
            status=models.status_code,
            count=len(listed),
            default_listed=default_listed,
            code=_error_code(models_body),
        )
    )
    failed = failed or not models_ok

    if environ_get("NEXUS_DOCTOR_SKIP_CHAT") == "1":
        lines.append("chat SKIP")
        return (1 if failed else 0), redact("\n".join(lines), secrets)

    payload = {
        "model": probe_model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "stream": False,
        "max_tokens": 16,
    }
    try:
        chat = await client.post("/v1/chat/completions", headers=headers, json=payload)
    except httpx.HTTPError:
        lines.append("chat FAIL unreachable")
        return 1, redact("\n".join(lines), secrets)

    try:
        chat_body = chat.json()
    except ValueError:
        chat_body = {}
    content = _assistant_text(chat_body)
    content_present = bool(redact(content, secrets).strip())
    chat_ok = chat.status_code == 200 and content_present
    lines.append(
        _line(
            "chat",
            chat_ok,
            status=chat.status_code,
            content_present=content_present,
            code=_error_code(chat_body),
        )
    )
    failed = failed or not chat_ok
    return (1 if failed else 0), redact("\n".join(lines), secrets)


async def run_probe(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str | None = None,
) -> tuple[int, str]:
    base = base_url or f"http://127.0.0.1:{configured_port()}"
    bridge_key = read_bridge_key()
    async with httpx.AsyncClient(
        transport=transport,
        base_url=base,
        trust_env=False,
        follow_redirects=False,
        timeout=_TIMEOUT,
    ) as client:
        return await execute_probe(client, bridge_key=bridge_key)


def main() -> None:
    code, report = asyncio.run(run_probe())
    print(report)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
