"""Startup refuses to open the door on a model no backend can serve.

A stranger who runs `pip install .`, pastes an OpenRouter key, and starts the
bridge should not end up with a green /health and a 404 on their first real
request. The packaged default has to be a public model id, and the door has to
check itself against the live catalog before it binds.

The rule these tests pin: only a *confirmed* absence stops startup. No key, a
backend that is not ready, and an upstream that could not be read are all
"unknowable" and must leave the bridge running.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from nexus.backends.openrouter import OpenRouterBackend
from nexus.config import DEFAULT_MODEL, model_for
from nexus.startup import (
    BackendCheck,
    DefaultModelUnavailable,
    check_default_model,
    configured_backends,
    enforce_default_model,
)

from tests.transport import HandlerTransport

ROOT = Path(__file__).resolve().parent.parent


def _catalog(*ids: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"id": model_id, "object": "model", "created": 1, "owned_by": "openrouter"}
                for model_id in ids
            ]
        },
    )


def _backend_catalog(*ids: str) -> OpenRouterBackend:
    return OpenRouterBackend(transport=HandlerTransport(lambda _r: _catalog(*ids)))


def test_shipped_default_is_a_public_openrouter_id() -> None:
    """The packaged default must be reachable by a stranger, not a private id."""
    assert DEFAULT_MODEL == "google/gemma-4-31b-it:free"
    assert re.fullmatch(r"[a-z0-9._-]+/[a-z0-9._:-]+", DEFAULT_MODEL), DEFAULT_MODEL
    assert DEFAULT_MODEL.startswith(("openai/", "anthropic/", "google/", "meta-llama/", "qwen/")), (
        "the shipped default should be a widely mirrored public model"
    )


def test_shipped_default_is_the_openrouter_default() -> None:
    """openrouter's row must not drift away from the constant."""
    assert model_for("openrouter") == DEFAULT_MODEL


def test_no_private_model_id_is_shipped_as_a_default() -> None:
    """The retired private id must not survive anywhere as a default."""
    offenders = [
        line
        for line in (ROOT / "src" / "nexus" / "config.py").read_text(encoding="utf-8").splitlines()
        if "stealth/space-bunny-alpha" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == [], offenders


@pytest.fixture
def use_backend(monkeypatch: pytest.MonkeyPatch):
    """Swap the backend builder the startup check uses."""
    import nexus.startup as startup_module

    def install(build) -> None:
        monkeypatch.setattr(startup_module, "build_backend", build)

    return install


def test_configured_backends_puts_the_active_one_first() -> None:
    assert configured_backends()[0] == "openrouter"
    assert "openrouter" in configured_backends()


def test_no_key_is_not_a_failure() -> None:
    """A backend that is not configured must never keep the bridge down."""
    checks = asyncio.run(enforce_default_model(["openrouter"]))
    assert checks, "expected a check result"
    assert all(c.reachable is not True for c in checks)


def test_missing_model_on_every_backend_is_fatal(use_backend, openrouter_key: str) -> None:
    """The shipped failure: no configured backend serves the default."""
    use_backend(lambda name: _backend_catalog("some/other-model"))

    with pytest.raises(DefaultModelUnavailable) as excinfo:
        asyncio.run(enforce_default_model(["openrouter", "openai"]))
    message = str(excinfo.value)
    assert DEFAULT_MODEL in message
    assert "NEXUS_MODEL" in message
    assert "openrouter" in message


def test_available_model_on_one_backend_is_enough(use_backend, openrouter_key: str) -> None:
    """One backend serving the default is enough to start."""
    use_backend(
        lambda name: (
            _backend_catalog(DEFAULT_MODEL)
            if name == "openrouter"
            else _backend_catalog("openai/gpt-4o-mini")
        )
    )

    checks = asyncio.run(enforce_default_model(["openrouter", "openai"]))
    assert [c.reachable for c in checks] == [True, False]


def test_unreadable_upstream_is_not_a_failure(use_backend, openrouter_key: str) -> None:
    """A network error is unknowable, not broken."""

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    use_backend(lambda name: OpenRouterBackend(transport=HandlerTransport(down)))

    checks = asyncio.run(enforce_default_model(["openrouter"]))
    assert all(c.reachable is not True for c in checks)


def test_error_names_every_backend_it_checked(use_backend, openrouter_key: str) -> None:
    use_backend(lambda name: _backend_catalog("unrelated/model"))

    with pytest.raises(DefaultModelUnavailable) as excinfo:
        asyncio.run(enforce_default_model(["openrouter", "openai", "xai"]))
    message = str(excinfo.value)
    for name in ("openrouter", "openai", "xai"):
        assert name in message
    assert "NOT AVAILABLE" in message


def test_backend_that_cannot_be_built_does_not_crash() -> None:
    """An unknown backend name is a skip, never an exception at startup."""
    checks = asyncio.run(enforce_default_model(["definitely-not-a-backend"]))
    assert checks[0].reachable is None


def test_confirmed_missing_is_strictly_false() -> None:
    """Only a positive absence disqualifies; None never does."""
    assert BackendCheck("openrouter", "m", False).confirmed_missing is True
    assert BackendCheck("openrouter", "m", None).confirmed_missing is False
    assert BackendCheck("openrouter", "m", True).confirmed_missing is False


def test_server_refuses_to_start_on_an_unreachable_default(tmp_path: Path) -> None:
    """End to end: the process exits nonzero and names the model."""
    script = (
        "import asyncio, sys\n"
        "from nexus import startup\n"
        "from nexus.backends.openrouter import OpenRouterBackend\n"
        "from tests.transport import HandlerTransport\n"
        "import httpx\n"
        "def handler(r):\n"
        "    return httpx.Response(200, json={'data': [\n"
        "        {'id': 'unrelated/model', 'object': 'model',\n"
        "         'created': 1, 'owned_by': 'openrouter'}]})\n"
        "startup.build_backend = lambda name=None: OpenRouterBackend(\n"
        "    transport=HandlerTransport(handler))\n"
        "from nexus.app import _enforce_default_model\n"
        "_enforce_default_model()\n"
        "print('SHOULD NOT REACH HERE')\n"
    )
    path = tmp_path / "run_startup.py"
    path.write_text(script, encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        "OPENROUTER_API_KEY": "test-key",
        "OS3_BRIDGE_MODEL": DEFAULT_MODEL,
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "SHOULD NOT REACH HERE" not in combined
    assert DEFAULT_MODEL in combined, combined
    assert "NEXUS_MODEL" in combined, combined


def _with_backend(build, names: list[str]) -> list[BackendCheck]:
    """Run the check against a stub builder without patching the module."""
    import nexus.startup as startup_module

    original = startup_module.build_backend
    startup_module.build_backend = build  # type: ignore[assignment]
    try:
        return asyncio.run(check_default_model(names))
    finally:
        startup_module.build_backend = original  # type: ignore[assignment]
