"""Point Rabbit OS3 at this door without copying a URL, key and model by hand.

Opt-in (``NEXUS_RABBIT_SETUP=on``) and built on Rabbit's undocumented web API, which
may change without notice. ``plan`` and ``status`` only read local state and are always
allowed. ``apply``, ``restore`` and ``verify`` drive a headed Chrome (see ``rabbit_cdp``)
and are refused while the opt-in is off.

The values are read here, never typed by the user: the bridge key from its file, the node
id from ``~/.rabbit-agent/connection.json``, the base URL from this door's port, and the
model from the live ``/v1/models`` list. A model that is not in that list is refused
before anything is deleted, because OS3 would refuse it too and a delete cannot be undone
without the old key.

Failure never leaves the user guessing. If the automatic path cannot start (no Chrome, not
signed in, a CloudFront block) the result is ``mode: "manual"`` with the exact values to
paste. If it fails after the old connection was deleted, the old one is put back from the
backup when that is possible, and the error says plainly whether it was.

Nothing returned, logged or written here contains the bridge key. The backup holds only
the masked key OS3 reports.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.auth import read_bridge_key
from nexus.backends.registry import build_backend
from nexus.config import (
    RABBIT_SETUP_ENV,
    bridge_key_path,
    configured_port,
    default_model,
    model_source,
    rabbit_backup_path,
    rabbit_cdp_port,
    rabbit_chrome_bin,
    rabbit_connection_path,
    rabbit_profile_dir,
    rabbit_setup_enabled,
)
from nexus.models_meta import billing
from nexus.rabbit_cdp import (
    SETTINGS_URL,
    ChromeCdp,
    ManualRequired,
    PageError,
)
from nexus.rabbit_keys import (
    MIN_PREFIX,
    KeyFileError,
    describe_candidates,
    find_restore_key,
    masked_key_matches,
    read_key_file,
)
from nexus.redact import redact

logger = logging.getLogger("nexus")

CONTEXT_WINDOW = 65536
FALLBACK_MODEL = "meituan/longcat-2.5-preview:free"
MAX_MODELS = 400
MODELS_TIMEOUT = 15.0
FEATURE = "os3"
BACKUP_VERSION = 1

ModelsSource = Callable[[], Awaitable[list[dict[str, Any]]]]
PulseSource = Callable[[], Awaitable[list[dict[str, Any]] | None]]


class RabbitError(Exception):
    """A refusal: nothing was changed. Renders as the dashboard error envelope."""

    def __init__(self, status: int, code: str, message: str, hint: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "code": self.code,
            "hint": self.hint,
            **self.extra,
        }


class _StepFailed(Exception):
    """A step after the delete did not work. Carries the cause code and a safe message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _StateUnknown(_StepFailed):
    """The delete was sent but OS3's state could not be read back, so it may be empty.

    ``settled_code`` is the code to report if a later read shows the old connection is
    still in place after all.
    """

    def __init__(self, message: str, settled_code: str) -> None:
        super().__init__("os3_state_unknown", message)
        self.settled_code = settled_code


def now_iso(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_url() -> str:
    return f"http://127.0.0.1:{configured_port()}/v1"


def read_node_id() -> str | None:
    try:
        raw = json.loads(rabbit_connection_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    node = raw.get("nodeId") if isinstance(raw, dict) else None
    if not isinstance(node, str) or not node.strip() or any(c.isspace() for c in node):
        return None
    return node.strip()


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic, mode 600 from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_backup() -> dict[str, Any] | None:
    try:
        raw = json.loads(rabbit_backup_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("version") == BACKUP_VERSION else None


def credential(data: Any) -> dict[str, Any] | None:
    """The credential from a byok/get answer (nested or top level), minus anything key-like."""
    if not isinstance(data, dict):
        return None
    cred = data["credential"] if "credential" in data else data
    if not isinstance(cred, dict) or not isinstance(cred.get("provider"), str):
        return None
    config = cred.get("config") if isinstance(cred.get("config"), dict) else {}
    status = cred.get("status") if isinstance(cred.get("status"), dict) else {}
    return {
        "provider": cred["provider"],
        "masked": cred.get("maskedKey") if isinstance(cred.get("maskedKey"), str) else None,
        "model": cred.get("model"),
        "tierModels": cred.get("tierModels"),
        "config": {
            "endpoint": config.get("endpoint"),
            "modelId": config.get("modelId") or cred.get("model"),
            "nodeId": config.get("nodeId"),
            "contextWindow": config.get("contextWindow"),
        },
        "status": {
            "lastCallOutcome": status.get("lastCallOutcome"),
            "lastCallAt": status.get("lastCallAt"),
        },
    }


def _brief(cred: dict[str, Any] | None) -> dict[str, Any] | None:
    if cred is None:
        return None
    return {
        "provider": cred["provider"],
        "endpoint": cred["config"].get("endpoint"),
        "modelId": cred["config"].get("modelId"),
        "masked": cred.get("masked"),
    }


def _masked(key: str) -> str:
    return "****" + key[-4:]


def _mask_matches(masked: str | None, key: str) -> bool:
    """True only when OS3's mask shows at least four trailing characters and ``key`` fits it."""
    return masked_key_matches(masked, key, min_prefix=0)


def _canonical(requested: str, ids: list[str]) -> str | None:
    """Exact id, or the one id that differs only in case. Never a fuzzy pick."""
    if requested in ids:
        return requested
    folded = [i for i in ids if i.lower() == requested.lower()]
    return folded[0] if len(folded) == 1 else None


def closest(requested: str, ids: list[str], limit: int = 5) -> list[str]:
    needle = requested.lower()
    by_lower = {i.lower(): i for i in ids}
    found = [i for low, i in by_lower.items() if needle and needle in low]
    for low in difflib.get_close_matches(needle, list(by_lower), n=limit, cutoff=0.5):
        if by_lower[low] not in found:
            found.append(by_lower[low])
    return (found or ids)[:limit]


async def live_models() -> list[dict[str, Any]]:
    """The rows /v1/models serves, from the active backend."""
    return await asyncio.wait_for(build_backend().list_models(), timeout=MODELS_TIMEOUT)


@dataclass
class _Inputs:
    requested: str | None
    rows: list[dict[str, Any]] | None = None
    models_error: str | None = None
    model: str | None = None
    model_source: str | None = None
    problems: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    key: str | None = None
    node_id: str | None = None

    @property
    def ids(self) -> list[str]:
        return [str(r["id"]) for r in self.rows or [] if isinstance(r.get("id"), str)]

    def problem(self, code: str, message: str, hint: str) -> None:
        self.problems.append({"code": code, "message": message, "hint": hint})


class RabbitSetup:
    def __init__(
        self,
        *,
        browser: Any | None = None,
        models_source: ModelsSource | None = None,
        pulse_source: PulseSource | None = None,
        clock: Callable[[], float] = time.time,
        retry_delay: float = 0.5,
    ) -> None:
        self._browser_override = browser
        self._models_source = models_source or live_models
        self._pulse_source = pulse_source
        self._clock = clock
        self._retry_delay = retry_delay

    def _browser(self, secrets: list[str] | None = None) -> Any:
        if self._browser_override is not None:
            return self._browser_override
        try:
            port = rabbit_cdp_port()
        except ValueError as exc:
            raise RabbitError(
                400, "invalid_config", str(exc), "Set NEXUS_RABBIT_CDP_PORT to a port number."
            ) from None
        return ChromeCdp(
            port=port,
            profile_dir=rabbit_profile_dir(),
            chrome_bin=rabbit_chrome_bin(),
            scrub=lambda text: redact(text, secrets or []),
        )

    @staticmethod
    def _require_opt_in() -> None:
        if not rabbit_setup_enabled():
            raise RabbitError(
                403,
                "opt_in_required",
                "Rabbit OS3 setup is off, so nothing was changed.",
                f"Set {RABBIT_SETUP_ENV}=on, restart the door if it runs as a service, and try "
                "again. `rabbit plan` shows what would happen without it.",
            )

    async def _inputs(self, requested: str | None) -> _Inputs:
        got = _Inputs(requested=requested.strip() if requested else None)
        try:
            got.rows = await self._models_source()
        except Exception as exc:
            got.models_error = type(exc).__name__
            got.problem(
                "models_unavailable",
                "The live model list could not be read, so a model cannot be validated.",
                "Run ./doctor.sh, fix the backend, then retry.",
            )
        got.key = read_bridge_key()
        if got.key is None:
            got.problem(
                "bridge_key_missing",
                f"No bridge key at {bridge_key_path()}.",
                "Start the door once (./nexus start) so it creates the key file.",
            )
        got.node_id = read_node_id()
        if got.rows is not None:
            self._choose(got)
        return got

    def _choose(self, got: _Inputs) -> None:
        ids = got.ids
        unverified = {str(r.get("id")) for r in got.rows or [] if "warning" in r}
        if not ids:
            got.problem(
                "models_unavailable",
                "The live model list is empty, so a model cannot be validated.",
                "Run ./doctor.sh, fix the backend, then retry.",
            )
            return
        if got.requested:
            chosen = _canonical(got.requested, ids)
            if chosen is None:
                self._unknown(got, got.requested, ids)
                return
            got.model, got.model_source = chosen, "requested"
        else:
            active = default_model()
            candidates: list[tuple[str, str]] = []
            if model_source() != "default":
                candidates.append((active, "active"))
            candidates += [(FALLBACK_MODEL, "fallback"), (active, "backend_default")]
            for name, source in candidates:
                found = _canonical(name, ids)
                if found is not None:
                    got.model, got.model_source = found, source
                    break
                if source == "active":
                    self._unknown(got, name, ids)
                    return
            if got.model is None:
                got.problem(
                    "model_required",
                    "No default model is in the live list.",
                    "Pass --model with one of: " + ", ".join(ids[:5]),
                )
                return
        if got.model in unverified:
            got.warnings.append(
                f"{got.model} is listed but the upstream did not confirm it; chat may fail."
            )
        note = billing(got.model)[1]
        if note:
            got.warnings.append(note)

    @staticmethod
    def _unknown(got: _Inputs, name: str, ids: list[str]) -> None:
        near = closest(name, ids)
        got.problem(
            "unknown_model",
            f"{name} is not in this door's live model list; OS3 would refuse it.",
            "Closest real ids: " + ", ".join(near),
        )
        got.problems[-1]["closest"] = ", ".join(near)

    def _manual_values(self, got: _Inputs) -> dict[str, Any]:
        return {
            "base_url": base_url(),
            "key_file": str(bridge_key_path()),
            "model_id": got.model,
            "context_window": CONTEXT_WINDOW,
        }

    async def status(self) -> dict[str, Any]:
        """Local state only. Never opens a browser or talks to Rabbit."""
        try:
            browser = self._browser()
            cdp = dict(browser.describe())
            try:
                cdp["listening"] = bool(await browser.listening())
            except Exception:
                cdp["listening"] = None
        except RabbitError as exc:
            cdp = {"error": exc.message, "listening": None}
        backup = load_backup()
        before = (backup or {}).get("before")
        return {
            "opt_in": rabbit_setup_enabled(),
            "opt_in_env": RABBIT_SETUP_ENV,
            "cdp": cdp,
            "node_id": {
                "present": read_node_id() is not None,
                "path": str(rabbit_connection_path()),
            },
            "key": {"path": str(bridge_key_path()), "present": read_bridge_key() is not None},
            "backup": {
                "path": str(rabbit_backup_path()),
                "present": backup is not None,
                "saved_at": (backup or {}).get("saved_at"),
                "previous_provider": before.get("provider") if isinstance(before, dict) else None,
            },
            "last_apply": (backup or {}).get("applied"),
            "settings_url": SETTINGS_URL,
        }

    async def plan(self, model: str | None = None) -> dict[str, Any]:
        """What apply would do. Reads the live model list and local files only."""
        got = await self._inputs(model)
        ids = got.ids
        rows = [
            {
                "id": i,
                "billing": billing(i)[0],
                "unverified": any(r.get("id") == i and "warning" in r for r in got.rows or []),
            }
            for i in ids[:MAX_MODELS]
        ]
        blocked = []
        if got.node_id is None:
            blocked.append(f"no node id in {rabbit_connection_path()}")
        return {
            "ok": not got.problems,
            "opt_in": rabbit_setup_enabled(),
            "opt_in_env": RABBIT_SETUP_ENV,
            "base_url": base_url(),
            "key_file": str(bridge_key_path()),
            "key_present": got.key is not None,
            "restore_key": describe_candidates(),
            "context_window": CONTEXT_WINDOW,
            "model": got.model,
            "model_source": got.model_source,
            "billing": billing(got.model)[0] if got.model else None,
            "warnings": got.warnings,
            "problems": got.problems,
            "models": rows,
            "models_truncated": len(ids) > MAX_MODELS,
            "automatic": {"possible": not blocked, "blocked_by": blocked},
            "manual": self._manual_values(got),
            "settings_url": SETTINGS_URL,
        }

    def _refuse_on_problems(self, got: _Inputs) -> None:
        if not got.problems:
            return
        first = got.problems[0]
        extra: dict[str, Any] = {}
        if first["code"] == "unknown_model":
            extra["closest"] = first.get("closest", "").split(", ")
        raise RabbitError(
            400 if first["code"] in {"unknown_model", "model_required"} else 409,
            first["code"],
            first["message"],
            first["hint"],
            **extra,
        )

    def _manual(
        self, action: str, got: _Inputs, code: str, message: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        logger.info("rabbit: %s falls back to manual (%s)", action, code)
        return {
            "ok": False,
            "action": action,
            "mode": "manual",
            "dry_run": dry_run,
            "code": code,
            "error": message,
            "hint": f"Open {SETTINGS_URL} and add a Local connection with the values below.",
            "manual": self._manual_values(got),
            "url": SETTINGS_URL,
            "warnings": got.warnings,
        }

    async def _current(self, page: Any) -> dict[str, Any] | None:
        reply = await page.call("/api/byok/get", {"feature": FEATURE})
        if not reply.ok:
            raise _StepFailed(
                "byok_get_failed", f"OS3 answered {reply.status} when reading the config."
            )
        return credential(reply.data)

    @staticmethod
    def _unlisted_local_model(cred: dict[str, Any] | None, ids: list[str]) -> str | None:
        """The model of a Local connection to this door that OS3 would refuse to register now.

        OS3 checks the model against the endpoint's live ``/v1/models`` on register. A
        Local connection to some other endpoint is judged by that endpoint, not this list.
        """
        if cred is None or cred["provider"] != "local" or not ids:
            return None
        cfg = cred["config"]
        model = cfg.get("modelId")
        if cfg.get("endpoint") != base_url() or not isinstance(model, str) or not model:
            return None
        return None if model in ids else model

    def _is_nexus(self, cred: dict[str, Any] | None, model: str | None, key: str | None) -> bool:
        if cred is None or cred["provider"] != "local":
            return False
        cfg = cred["config"]
        return (
            cfg.get("endpoint") == base_url()
            and (model is None or cfg.get("modelId") == model)
            and (key is None or _mask_matches(cred.get("masked"), key))
        )

    async def apply(
        self,
        model: str | None = None,
        dry_run: bool = False,
        accept_no_rollback: bool = False,
    ) -> dict[str, Any]:
        self._require_opt_in()
        got = await self._inputs(model)
        self._refuse_on_problems(got)
        assert got.model is not None and got.key is not None
        secrets = [got.key]
        chosen, key = got.model, got.key
        if got.node_id is None:
            return self._manual(
                "apply",
                got,
                "rabbit_agent_missing",
                f"No rabbit-agent node id in {rabbit_connection_path()}.",
                dry_run=dry_run,
            )
        logger.info("rabbit: apply start model=%s dry_run=%s", chosen, dry_run)
        try:
            async with self._browser(secrets).open_page() as page:
                result = await self._apply_on_page(
                    page, got, chosen, key, dry_run, accept_no_rollback
                )
        except ManualRequired as exc:
            return self._scrub(
                self._manual("apply", got, exc.code, exc.message, dry_run=dry_run), secrets
            )
        except (PageError, _StepFailed) as exc:
            code = exc.code if isinstance(exc, _StepFailed) else "page_failed"
            message = exc.message if isinstance(exc, _StepFailed) else str(exc)
            return self._scrub(self._manual("apply", got, code, message, dry_run=dry_run), secrets)
        return self._scrub(result, secrets)

    async def _apply_on_page(
        self,
        page: Any,
        got: _Inputs,
        model: str,
        key: str,
        dry_run: bool,
        accept_no_rollback: bool = False,
    ) -> dict[str, Any]:
        node_id = got.node_id
        before = await self._current(page)
        after = {
            "provider": "local",
            "endpoint": base_url(),
            "modelId": model,
            "contextWindow": CONTEXT_WINDOW,
            "key": _masked(key),
        }
        warnings = list(got.warnings)
        restore_key = None
        if before is not None and before["provider"] != "local":
            restore_key = find_restore_key(before.get("masked"))
            warnings.append(
                f"OS3 currently uses {before['provider']}; that connection is replaced."
                + (
                    ""
                    if restore_key.usable or restore_key.source == "env"
                    else " Set NEXUS_RABBIT_RESTORE_KEY_FILE if you want it restorable."
                )
            )
            warnings.extend(restore_key.warnings())
        no_rollback = self._unlisted_local_model(before, got.ids)
        if no_rollback:
            warnings.append(
                f"OS3 currently uses the model {no_rollback}, which this door does not list, so "
                "OS3 would refuse to register it again. If this change fails after the delete, "
                "the current connection cannot be put back."
            )
        result: dict[str, Any] = {
            "ok": True,
            "action": "apply",
            "mode": "auto",
            "dry_run": dry_run,
            "model": model,
            "billing": billing(model)[0],
            "warnings": warnings,
            "diff": {"before": _brief(before), "after": after},
        }
        if restore_key is not None:
            result["restore_key"] = restore_key.as_dict()
        if dry_run:
            result["changed"] = False
            return result
        if self._is_nexus(before, model, key):
            result["changed"] = False
            result["note"] = "OS3 already points at this door with this model; nothing to do."
            self._record_applied(before, model)
            return result

        if no_rollback and not accept_no_rollback:
            raise RabbitError(
                409,
                "rollback_unavailable",
                f"Nothing was changed. OS3 uses the model {no_rollback}, which this door does "
                "not list, so OS3 would refuse it if setup had to put it back after a failure. "
                "Setup could leave OS3 with no provider.",
                f"Add the values in `manual` at {SETTINGS_URL} yourself, or pass "
                "accept_no_rollback (CLI: --accept-no-rollback) to go ahead without a rollback.",
                manual=self._manual_values(got),
                url=SETTINGS_URL,
                diff=result["diff"],
            )

        existing = load_backup()
        keep = self._is_nexus(before, None, None) and existing is not None
        if not keep:
            write_private_json(
                rabbit_backup_path(),
                {"version": BACKUP_VERSION, "saved_at": now_iso(self._clock), "before": before},
            )
        result["backup"] = {"path": str(rabbit_backup_path()), "kept_existing": keep}

        body = {
            "provider": "local",
            "feature": FEATURE,
            "nodeId": node_id,
            "endpoint": base_url(),
            "modelId": model,
            "contextWindow": CONTEXT_WINDOW,
        }
        cause: _StepFailed | ManualRequired | PageError
        delete_ran = False
        try:
            if before is not None:
                delete_ran = True
                try:
                    await self._delete(page, before)
                except _StateUnknown:
                    raise
                except _StepFailed:
                    delete_ran = False
                    raise
            reply = await page.register(body, key)
            if not reply.ok:
                raise _StepFailed(
                    "register_failed", f"OS3 answered {reply.status} to the register."
                )
        except (_StepFailed, ManualRequired, PageError) as exc:
            cause = exc
        else:
            return await self._finish_apply(page, result, model, key)
        return await self._failed(page, result, before, cause, delete_ran, got)

    async def _delete(self, page: Any, cred: dict[str, Any]) -> None:
        """Delete ``cred`` and confirm it is gone.

        Returns when the old connection is gone. Raises _StepFailed when a read-back showed
        it is still there, and _StateUnknown when OS3's state could not be read.

        Request shapes, from scripts that got 200 from os3.rabbit.tech:
        Local is {provider: "local", feature, key: "local"}. Custom is {provider, feature}
        with no key; that was confirmed live (Custom Nous to Local). The retry with
        key=provider on a 4xx is a guess for providers that want a key, tried once.
        """
        provider = cred["provider"]
        body: dict[str, Any] = {"provider": provider, "feature": FEATURE}
        if provider == "local":
            body["key"] = "local"
        reply = await page.call("/api/byok/delete", body)
        retried = provider != "local" and 400 <= reply.status < 500
        if retried:
            reply = await page.call("/api/byok/delete", {**body, "key": provider})
        if not reply.ok and not retried:
            raise _StepFailed("delete_failed", f"OS3 answered {reply.status} to the delete.")
        present = await self._still_there(page, provider)
        if not reply.ok:
            said = f"OS3 answered {reply.status} to the delete, twice."
            if present is None:
                raise _StateUnknown(
                    f"{said} Reading the config back failed, so it is not known whether "
                    "the connection is gone.",
                    "delete_failed",
                )
            if present:
                raise _StepFailed("delete_failed", said)
            return
        if present is None:
            raise _StateUnknown(
                "OS3 reported the delete but reading the config back failed.", "delete_unconfirmed"
            )
        if present:
            raise _StepFailed(
                "delete_unconfirmed",
                f"OS3 reported the delete but the {provider} connection is still there.",
            )

    async def _still_there(self, page: Any, provider: str) -> bool | None:
        """Whether ``provider`` is still registered, or None when the read-back failed."""
        try:
            left = await self._current(page)
        except (_StepFailed, ManualRequired, PageError):
            return None
        return left is not None and left["provider"] == provider

    async def _finish_apply(
        self, page: Any, result: dict[str, Any], model: str, key: str
    ) -> dict[str, Any]:
        problem = None
        cred = None
        unconfirmed = False
        for attempt in range(3):
            unconfirmed = False
            try:
                cred = await self._current(page)
            except (ManualRequired, PageError) as exc:
                problem, unconfirmed = str(exc), True
            except _StepFailed as exc:
                problem = str(exc)
            else:
                problem = self._mismatch(cred, model, key)
                if problem is None:
                    break
            if attempt < 2:
                await asyncio.sleep(self._retry_delay)
        if problem is not None and unconfirmed:
            result["warnings"] = [
                *result["warnings"],
                "verify_unconfirmed: OS3 accepted the registration, but reading it back failed "
                f"({problem}). Run ./nexus rabbit verify to confirm.",
            ]
            result.update(changed=True, verify="unconfirmed")
            self._record_applied(None, model)
            return result
        if problem is not None:
            result.update(
                ok=False,
                changed=True,
                code="verify_failed",
                error=f"OS3 accepted the registration but reads back differently: {problem}.",
                hint="Run ./nexus rabbit verify, or ./nexus rabbit restore.",
            )
            return result
        result.update(changed=True, verified=_brief(cred))
        self._record_applied(cred, model)
        return result

    @staticmethod
    def _mismatch(cred: dict[str, Any] | None, model: str, key: str | None = None) -> str | None:
        if cred is None:
            return "no connection is registered"
        cfg = cred["config"]
        if cred["provider"] != "local":
            return f"provider is {cred['provider']}, not local"
        if cfg.get("endpoint") != base_url():
            return "endpoint differs"
        if cfg.get("modelId") != model:
            return f"model is {cfg.get('modelId')}, not {model}"
        return None

    def _record_applied(self, cred: dict[str, Any] | None, model: str) -> None:
        backup = load_backup()
        if backup is None:
            return
        backup["applied"] = {
            "at": now_iso(self._clock),
            "model": model,
            "endpoint": base_url(),
            "context_window": CONTEXT_WINDOW,
            "masked": (cred or {}).get("masked"),
        }
        write_private_json(rabbit_backup_path(), backup)

    async def _failed(
        self,
        page: Any,
        result: dict[str, Any],
        before: dict[str, Any] | None,
        cause: _StepFailed | ManualRequired | PageError,
        delete_ran: bool,
        got: _Inputs | None = None,
    ) -> dict[str, Any]:
        code = cause.code if isinstance(cause, (_StepFailed, ManualRequired)) else "page_failed"
        message = cause.message if isinstance(cause, (_StepFailed, ManualRequired)) else str(cause)
        outcome, detail, restore_warnings = await self._recover(page, before, delete_ran)
        result.update(ok=False, changed=outcome != "unchanged", mode="auto")
        result["warnings"] = [*result["warnings"], *restore_warnings]
        result.pop("verified", None)
        result["cause"] = {"code": code, "message": message}
        result["restore"] = {"outcome": outcome, "detail": detail}
        if outcome == "restored":
            result.update(
                code="restore_attempted",
                error=f"The change failed ({message}) after the old connection was removed. "
                "The previous OS3 connection was put back and checked.",
                hint="Nothing else to do; fix the cause above and run setup again.",
            )
        elif outcome == "failed" and isinstance(cause, _StateUnknown):
            result.update(
                code="os3_state_unknown",
                error=f"OS3 may have no provider connected. {message} The previous connection "
                f"could NOT be put back: {detail}",
                hint=f"Re-add a provider at {SETTINGS_URL} with the values in `manual`, or use "
                "./nexus rabbit restore once the cause is fixed.",
                **self._unknown_extras(got),
            )
        elif outcome == "failed":
            result.update(
                code="restore_failed",
                error=f"The change failed ({message}) after the old connection was removed, "
                f"and it could NOT be put back: {detail}",
                hint=f"OS3 may have no provider now. Re-add one at {SETTINGS_URL}, or use "
                "./nexus rabbit restore once the cause is fixed.",
            )
        elif isinstance(cause, _StateUnknown):
            result.update(
                code=cause.settled_code,
                error=f"{message} A later read shows the previous connection is still in place.",
                hint="Fix the cause above and run setup again.",
            )
        else:
            result.update(
                code=code,
                error=message + (" The previous OS3 connection is unchanged." if before else ""),
                hint="Fix the cause above and run setup again.",
            )
        logger.error("rabbit: apply failed code=%s restore=%s", result["code"], outcome)
        return result

    def _unknown_extras(self, got: _Inputs | None) -> dict[str, Any]:
        return {"manual": self._manual_values(got or _Inputs(requested=None)), "url": SETTINGS_URL}

    async def _recover(
        self, page: Any, before: dict[str, Any] | None, delete_ran: bool
    ) -> tuple[str, str, list[str]]:
        """Put ``before`` back if it is gone. Returns (outcome, detail, warnings)."""
        if not delete_ran:
            return "unchanged", "The previous connection was never deleted.", []
        try:
            current = await self._current(page)
        except (_StepFailed, ManualRequired, PageError):
            current = False
        if current is not False:
            if before is None and current is None:
                return "unchanged", "OS3 had no connection before and has none now.", []
            if before is not None and current is not None and self._same(current, before):
                return "unchanged", "The previous connection is still in place.", []
        if before is None:
            return "unchanged", "OS3 had no connection before.", []
        try:
            warnings = await self._reregister(page, before)
        except (_StepFailed, ManualRequired, PageError) as exc:
            return "failed", exc.message if hasattr(exc, "message") else str(exc), []
        return "restored", "The previous connection was re-registered and read back.", warnings

    @staticmethod
    def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return _brief(a) == _brief(b)

    def _restore_body(self, before: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The register body and key to put ``before`` back, or _StepFailed saying why not."""
        cfg = before["config"]
        if before["provider"] == "local" and cfg.get("endpoint") == base_url():
            key = read_bridge_key()
            if key is None:
                raise _StepFailed(
                    "restore_failed", f"the bridge key file {bridge_key_path()} is gone"
                )
        else:
            masked = before.get("masked")
            found = find_restore_key(masked)
            path = found.path
            if path is None:
                raise _StepFailed(
                    "restore_failed",
                    f"the previous {before['provider']} connection used a key this bridge does "
                    "not hold, NEXUS_RABBIT_RESTORE_KEY_FILE is not set, and "
                    + (
                        "OS3's masked key shows too little of it to pick a key file safely"
                        if found.mask_too_short
                        else "no single key file in the key dirs matches it"
                    ),
                )
            try:
                key = read_key_file(path)
            except KeyFileError as exc:
                raise _StepFailed("restore_failed", str(exc)) from None
            min_prefix = 0 if found.source == "env" else MIN_PREFIX
            if not masked_key_matches(masked, key, min_prefix=min_prefix):
                raise _StepFailed(
                    "restore_failed", f"{path} does not match the previous key OS3 reported"
                )
        local = before["provider"] == "local"
        body: dict[str, Any] = {"provider": "local" if local else "custom", "feature": FEATURE}
        if local:
            for name in ("nodeId", "endpoint", "modelId", "contextWindow"):
                if cfg.get(name) is not None:
                    body[name] = cfg[name]
            body.setdefault("nodeId", read_node_id())
            body.setdefault("contextWindow", CONTEXT_WINDOW)
        else:
            # OS3 stores OpenRouter and Nous as provider "custom" plus an endpoint, so every
            # non-local backup is registered that way: {provider, feature, key, endpoint}.
            endpoint = cfg.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise _StepFailed(
                    "restore_failed",
                    f"the saved {before['provider']} connection has no endpoint to register",
                )
            body["endpoint"] = endpoint
        return body, key

    async def _reregister(self, page: Any, before: dict[str, Any]) -> list[str]:
        """Register ``before`` again and read it back. Returns warnings for tiers not restored."""
        body, key = self._restore_body(before)
        reply = await page.register(body, key)
        if not reply.ok:
            raise _StepFailed("restore_failed", f"OS3 answered {reply.status} to the re-register")
        raw = before.get("tierModels")
        tiers = (
            {t: m for t, m in raw.items() if isinstance(t, str) and isinstance(m, str)}
            if isinstance(raw, dict)
            else {}
        )
        unset: dict[str, str] = {}
        for tier, model in tiers.items():
            try:
                tier_reply = await page.call(
                    "/api/byok/update-model", {"feature": FEATURE, "tier": tier, "model": model}
                )
            except (ManualRequired, PageError):
                unset[tier] = model
                continue
            if not tier_reply.ok:
                unset[tier] = model
        after = await self._current(page)
        # A custom register carries no model id, so only Local is read back by model.
        shows_before = after is not None and (
            self._same_target(after, before)
            if before["provider"] == "local"
            else after["provider"] == body["provider"]
            and after["config"].get("endpoint") == before["config"].get("endpoint")
        )
        if after is None or not shows_before:
            raise _StepFailed("restore_failed", "OS3 does not show the previous connection")
        shown = after.get("tierModels") if isinstance(after.get("tierModels"), dict) else {}
        for tier, model in tiers.items():
            if shown.get(tier) != model:
                unset[tier] = model
        if not unset:
            return []
        by_hand = ", ".join(f"{tier}={model}" for tier, model in unset.items())
        return [
            f"tiers_not_restored: the connection is back but these tier models were not; set "
            f"them by hand in OS3: {by_hand}."
        ]

    @staticmethod
    def _same_target(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return (
            a["provider"] == b["provider"]
            and a["config"].get("endpoint") == b["config"].get("endpoint")
            and a["config"].get("modelId") == b["config"].get("modelId")
        )

    async def restore(self) -> dict[str, Any]:
        self._require_opt_in()
        backup = load_backup()
        if backup is None:
            raise RabbitError(
                409,
                "no_backup",
                "There is no saved OS3 connection to restore.",
                f"A backup is written at {rabbit_backup_path()} the first time setup changes OS3.",
            )
        before = backup.get("before")
        if before is not None and not (isinstance(before, dict) and "provider" in before):
            raise RabbitError(
                409, "no_backup", "The backup file is unreadable.", f"Check {rabbit_backup_path()}."
            )
        got = _Inputs(requested=None)
        got.key = read_bridge_key()
        got.node_id = read_node_id()
        applied = backup.get("applied")
        got.model = applied.get("model") if isinstance(applied, dict) else None
        secrets = [got.key] if got.key else []
        logger.info("rabbit: restore start")
        try:
            async with self._browser(secrets).open_page() as page:
                result = await self._restore_on_page(page, before, got)
        except ManualRequired as exc:
            return self._scrub(self._manual("restore", got, exc.code, exc.message), secrets)
        except (PageError, _StepFailed) as exc:
            code = exc.code if isinstance(exc, _StepFailed) else "page_failed"
            message = exc.message if isinstance(exc, _StepFailed) else str(exc)
            return self._scrub(self._manual("restore", got, code, message), secrets)
        return self._scrub(result, secrets)

    async def _restore_on_page(
        self, page: Any, before: dict[str, Any] | None, got: _Inputs | None = None
    ) -> dict[str, Any]:
        current = await self._current(page)
        result: dict[str, Any] = {
            "ok": True,
            "action": "restore",
            "mode": "auto",
            "diff": {"before": _brief(current), "after": _brief(before)},
            "warnings": [],
        }
        if (before is None and current is None) or (
            before is not None and current is not None and self._same_target(current, before)
        ):
            result["changed"] = False
            result["note"] = "OS3 already matches the backup; nothing to do."
            return result
        if before is None and not self._is_nexus(current, None, None):
            raise RabbitError(
                409,
                "restore_refused",
                "The backup says OS3 had no connection, but the current one is not this door's.",
                "Remove it in OS3 yourself; it was not created by this bridge.",
            )
        if before is not None:
            try:
                self._restore_body(before)
            except _StepFailed as exc:
                result.update(
                    ok=False,
                    changed=False,
                    code="restore_failed",
                    error="Nothing was changed: the previous connection cannot be put back, "
                    f"{exc.message}.",
                    hint="Set NEXUS_RABBIT_RESTORE_KEY_FILE, or re-add the provider at "
                    f"{SETTINGS_URL}.",
                )
                return result
            refusal = await self._restore_preflight(before)
            if refusal is not None:
                code, message = refusal
                result.update(
                    ok=False,
                    changed=False,
                    code=code,
                    error=f"Nothing was changed: {message}",
                    hint="The saved connection cannot be registered in OS3 right now, so "
                    "deleting the current one would leave OS3 with no provider. Re-add the "
                    f"previous provider at {SETTINGS_URL} yourself.",
                )
                return result
        deleted = False
        try:
            if current is not None:
                deleted = True
                try:
                    await self._delete(page, current)
                except _StateUnknown:
                    raise
                except _StepFailed:
                    deleted = False
                    raise
            if before is not None:
                result["warnings"].extend(await self._reregister(page, before))
        except (_StepFailed, ManualRequired, PageError) as exc:
            message = exc.message if hasattr(exc, "message") else str(exc)
            put_back = await self._put_current_back(page, current) if deleted else None
            tail = {
                True: " The connection that was in place before this restore was put back.",
                False: " The connection that was in place before this restore could NOT be "
                "put back either.",
                None: "",
            }[put_back]
            if not deleted:
                hint = (
                    f"OS3 still has its current connection. Fix the cause, or see {SETTINGS_URL}."
                )
            elif put_back:
                hint = (
                    "OS3 is back on the connection it had before this restore. Fix the cause "
                    f"and retry, or re-add the previous provider at {SETTINGS_URL}."
                )
            else:
                hint = f"OS3 may have no provider now. Re-add one at {SETTINGS_URL}."
            result.update(
                ok=False,
                changed=deleted and not put_back,
                code="restore_failed",
                current_put_back=put_back,
                cause={"code": getattr(exc, "code", "page_failed"), "message": message},
                error=f"The previous OS3 connection could NOT be put back: {message}.{tail}",
                hint=hint,
            )
            if isinstance(exc, _StateUnknown) and not put_back:
                result.update(
                    code="os3_state_unknown",
                    error=f"OS3 may have no provider connected. {message} The connection that "
                    "was in place before this restore could NOT be put back either.",
                    hint=f"Re-add a provider at {SETTINGS_URL} with the values in `manual`.",
                    **self._unknown_extras(got),
                )
            return result
        result["changed"] = True
        saved = load_backup()
        if saved is not None:
            saved.update(restored_at=now_iso(self._clock), applied=None)
            write_private_json(rabbit_backup_path(), saved)
        return result

    async def _restore_preflight(self, before: dict[str, Any]) -> tuple[str, str] | None:
        """Why registering ``before`` would be refused, checked before anything is deleted."""
        if before.get("provider") != "local":
            return None
        cfg = before.get("config") if isinstance(before.get("config"), dict) else {}
        if not (cfg.get("nodeId") or read_node_id()):
            return (
                "restore_node_missing",
                f"the saved Local connection has no node id and none is in "
                f"{rabbit_connection_path()}.",
            )
        if cfg.get("endpoint") != base_url():
            return None
        model = cfg.get("modelId")
        try:
            rows = await self._models_source()
        except Exception as exc:
            return (
                "restore_model_unavailable",
                f"the live model list could not be read ({type(exc).__name__}), so the saved "
                f"model {model} cannot be checked.",
            )
        ids = [str(r["id"]) for r in rows if isinstance(r.get("id"), str)]
        if isinstance(model, str) and model and model not in ids:
            return (
                "restore_model_unavailable",
                f"the saved model {model} is not in this door's live model list, so OS3 would "
                "refuse to register it.",
            )
        return None

    async def _put_current_back(self, page: Any, current: dict[str, Any] | None) -> bool:
        """After a failed restore, re-register the connection that restore just deleted."""
        if current is None or current.get("provider") != "local":
            return False
        try:
            now = await self._current(page)
        except (_StepFailed, ManualRequired, PageError):
            now = None
        if now is not None and self._same(now, current):
            return True
        try:
            await self._reregister(page, current)
        except (_StepFailed, ManualRequired, PageError):
            return False
        return True

    async def verify(self) -> dict[str, Any]:
        self._require_opt_in()
        got = await self._inputs(None)
        backup = load_backup() or {}
        applied = backup.get("applied") if isinstance(backup.get("applied"), dict) else None
        expect = applied.get("model") if applied else None
        try:
            async with self._browser().open_page() as page:
                cred = await self._current(page)
        except ManualRequired as exc:
            return self._manual("verify", got, exc.code, exc.message)
        except (PageError, _StepFailed) as exc:
            code = exc.code if isinstance(exc, _StepFailed) else "page_failed"
            message = exc.message if isinstance(exc, _StepFailed) else str(exc)
            return self._manual("verify", got, code, message)
        ids = got.ids
        model_now = cred["config"].get("modelId") if cred else None
        checks = {
            "provider_local": bool(cred and cred["provider"] == "local"),
            "endpoint_matches": bool(cred and cred["config"].get("endpoint") == base_url()),
            "model_matches_applied": None if expect is None else model_now == expect,
            "model_in_live_list": None if not ids or model_now is None else model_now in ids,
        }
        ok = (
            checks["provider_local"]
            and checks["endpoint_matches"]
            and checks["model_matches_applied"] is not False
            and checks["model_in_live_list"] is not False
        )
        return {
            "ok": ok,
            "action": "verify",
            "mode": "auto",
            "current": _brief(cred),
            "checks": checks,
            "call_status": (cred or {}).get("status"),
            "nexus_saw_request": await self._saw_request((applied or {}).get("at")),
            "applied_at": (applied or {}).get("at"),
            **(
                {}
                if ok
                else {
                    "code": "verify_failed",
                    "error": "OS3 is not set up to use this door.",
                    "hint": "Run ./nexus rabbit setup.",
                }
            ),
        }

    async def _saw_request(self, since: str | None) -> dict[str, Any] | None:
        if self._pulse_source is None or since is None:
            return None
        try:
            entries = await self._pulse_source()
        except Exception:
            return None
        if entries is None:
            return None
        recent = [e for e in entries if isinstance(e, dict) and str(e.get("ts", "")) >= since]
        return {
            "since": since,
            "v1": bool(recent),
            "chat": any(str(e.get("route", "")).startswith("POST /v1/") for e in recent),
        }

    @staticmethod
    def _scrub(result: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
        text = redact(json.dumps(result), [s for s in secrets if s])
        return json.loads(text)
