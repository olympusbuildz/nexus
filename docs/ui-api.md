# UI API contract (Nexus)

Developer reference. This page is for people building or changing the web UI in `ui/`; it is not needed to use Nexus. It describes the JSON that Nexus serves outside `/v1`. Field names and shapes are copied from the code and from a running Nexus, and are covered by tests (`tests/test_nexus_api.py`, `tests/test_ui_static.py`). Items that first shipped after this page was written say "added with `ui_api: 1`". A Nexus that lacks `ui_api` in `GET /health` does not have them; see [Versioning](#versioning-and-stability).

Nothing here changes `127.0.0.1:11435`, the LaunchAgent, or the `/v1` surface Rabbit OS3
uses. `/v1/*` is frozen and is only documented where the UI has to relate to it.

Settings are `NEXUS_*` and state files are `.nexus_*`; older names are covered in [upgrading-from-os3-bridge.md](upgrading-from-os3-bridge.md). `GET /health` reports `"service": "nexus"` (older processes reported `"os3-bridge"`).
Paths keep the existing `/dashboard/*` prefix rather than introducing a parallel `/api/*` tree.

## Index

| Need | Endpoint |
| --- | --- |
| [Backends status](#1-backends-status) | `GET /health` → `backends` |
| [Model list](#2-model-list) | `GET /dashboard/models?backend=<id>` |
| [Recent calls](#3-recent-calls) | `GET /dashboard/pulse` |
| [Config read](#41-get-dashboardconfig) | `GET /dashboard/config` |
| [Config write, key write](#42-post-dashboardconfig) | `POST /dashboard/config` |
| [Bench](#5-bench-run) | `POST /dashboard/bench` |
| [Serving the UI](#7-serving-the-ui) | `GET /` and static assets |
| [Bridge key](#6-auth-and-the-same-origin-guard) | `GET /dashboard/bridge-key` |
| [Rabbit OS3 setup](#8-rabbit-os3-setup) | `GET /dashboard/rabbit`, `POST /dashboard/rabbit/apply`, `POST /dashboard/rabbit/restore` |
| Diagnostics (superset of health) | `GET /doctor` |
| OpenAI surface | `/v1/*` |

Everything below `/dashboard/*`, plus `/`, `/health`, and `/doctor`, is reached
same-origin and needs no bearer token. Only `/v1/*` needs the Nexus key.

## Conventions

- **Base URL:** `http://127.0.0.1:11435`. The UI is served by Nexus at `/` and uses
  relative URLs. Never hard-code `localhost` (the bind is `127.0.0.1` only).
- **Content type:** responses are `application/json`. Writes must be sent as
  `Content-Type: application/json` (the guard refuses anything else).
- **Caching:** send `fetch(url, { cache: "no-store" })` as the existing page does. The
  bridge sets `Cache-Control: no-store` on `/dashboard/models` and `/dashboard/bridge-key`.
- **Polling:** the existing dashboard polls `/health` and `/dashboard/pulse` every 5 s.
  That is the supported cadence; do not poll faster (see caching per section).
- **Success shape:** `/health`, `/doctor`, `/dashboard/pulse` return bare objects (no
  `ok` wrapper beyond `/health`'s own `ok: true`). `/dashboard/models` returns a bare
  object on success. `POST /dashboard/config`, `GET /dashboard/config` and
  `POST /dashboard/bench` return `{"ok": true, ...}`.
- **No secrets, ever.** No response below contains a provider key or (outside
  `GET /dashboard/bridge-key`) the Nexus key. Do not add one.

## Error envelope

Three error shapes exist. The UI has to tolerate all of them.

| Where | Shape | Notes |
| --- | --- | --- |
| `/v1/*` | `{"error": {"message", "type", "code", "hint", ...}}` (OpenAI shape plus `hint`) | Frozen |
| Everything Nexus itself refuses or fails outside `/v1`: `/dashboard/*` handlers, the same-origin guard, an unexpected 500 | `{"ok": false, "error": "<message string>", "code": "<snake_case>", "hint": "<fix>"}` | `code` and `hint` added with `ui_api: 1` |
| Unrouted method or path outside `/v1` (framework default) | `{"detail": "Method Not Allowed"}` / `{"detail": "Not Found"}` | Framework default, left as is |

### One dashboard envelope

Every non-2xx response outside `/v1` that Nexus produces itself:

```json
{
  "ok": false,
  "error": "No OpenRouter key file yet. Type a key, or run ./nexus setup --backend openrouter --api-key-stdin",
  "code": "key_missing",
  "hint": "Paste an OpenRouter key in the Key field and save, or put one in a mode-600 file at /home/<user>/.config/nexus/openrouter.key."
}
```

- `ok` and `error` are unchanged: `error` stays a **string**, so a consumer that only reads
  those two fields keeps working. This is a strict superset of today's body.
- `code` is a stable `snake_case` identifier the UI may branch on.
- `hint` is a plain-English fix a stranger can act on. It matches the repo rule that
  error bodies carry a usable `hint`. `/v1` errors already do (`src/nexus/errors.py`,
  `fallback_hint`).
- `/v1` keeps its own envelope. The UI never calls `/v1` for control-plane data.

The framework's own `{"detail": ...}` 404 and 405 are not wrapped. The UI should treat a
non-2xx body without `ok: false` as `code: "http_<status>"` with `hint` equal to
`detail`, and the same for a Nexus that predates `ui_api`.

An unexpected exception outside `/v1` is `500` with `code: "internal_error"`; the message
is always `Internal error`, never the exception text. (Before `ui_api: 1` this was the
OpenAI-shaped `{"error": {...}}`. Inside `/v1` it still is.)

Codes (HTTP status in parentheses; the statuses the routes already used are kept):

| code | HTTP | Raised by |
| --- | --- | --- |
| `host_refused` | 403 | guard: `Host` is not `127.0.0.1`, `localhost`, or `[::1]` |
| `cross_site_refused` | 403 | guard: write with `Sec-Fetch-Site` other than `same-origin`/`none` |
| `origin_mismatch` | 403 | guard: write whose `Origin` is not Nexus |
| `json_required` | 403 | guard: write whose content type is not `application/json` |
| `invalid_json` | 400 | body is not a JSON object |
| `invalid_request` | 400 | wrong field type, empty backend, bad model id, etc. |
| `unknown_backend` | 400 (config, bench) / 404 (models) | name is not one of the ten backend ids |
| `key_missing` | 400 | API backend chosen, no key typed, no key file; or `activate: false` with no key |
| `key_invalid` | 400 | key has whitespace, is too long, or looks like a placeholder |
| `key_rejected` | 400 | the provider answered 401/403 to the typed key |
| `key_unverified` | 400 | the provider could not be reached to check the key |
| `cli_missing` | 400 | CLI backend chosen and its binary is not on `PATH` |
| `not_ready` | 409 | activating a backend whose `/health` row is not ready (CLI not logged in, no key). Choice and model files are left as they were |
| `cli_has_no_key` | 400 | a key was sent for a subscription CLI backend, or `activate: false` named one |
| `write_failed` | 400 | could not write or `chmod 600` a file |
| `bench_busy` | 409 | a bench is already running |
| `opt_in_required` | 403 | a Rabbit OS3 write while `NEXUS_RABBIT_SETUP` is not on |
| `rabbit_busy` | 409 | a Rabbit OS3 change is already running |
| `unknown_model` | 400 | Rabbit apply: the model is not in the live `/v1/models` list (`closest` lists real ids) |
| `bench_limits` | 400 | bench over the target, prompt, token or timeout caps |
| `internal_error` | 500 | unexpected failure outside `/v1` |

## 1. Backends status

**`GET /health`** (reads `backends`). Source: `src/nexus/status.py`
(`health_payload`, `describe_catalog` from `src/nexus/backends/registry.py`). No
authentication. There is no separate `/dashboard/backends` endpoint: this response
already carries one row per backend, and the existing dashboard renders from it.

`backends` is an object keyed by backend id: `openrouter`, `openai`, `anthropic`, `xai`,
`nous` (REST, key-based) and `codex`, `claude-code`, `copilot`, `grok-build`,
`antigravity` (subscription CLIs). Top-level fields the UI also needs: `backend` (the
**active** backend id), `backend_source` (`"file"` when Settings or setup saved a choice,
`"env"` when only `NEXUS_BACKEND` is set, `"default"` otherwise), `ui_api` (the integer contract version, `1`), `ready` (would chat work right now), `model`, `model_ready`
(`true`/`false`/`null` = could not be confirmed), `model_hint`, `upstream_status`
(only when the provider refused the key), `restart_*`, and `watchdog` (`enabled`, `last_check`,
`last_restart`, `restart_count`, `last_reason`; present only when `NEXUS_RABBIT_WATCHDOG` is set).

### Row fields that exist

Every row:

| field | type | meaning |
| --- | --- | --- |
| `id` | string | canonical backend id (aliases such as `claude`, `grok` are not row keys) |
| `role` | string | `primary` (openrouter, nous), `optional` (openai, anthropic, xai), `subscription` (CLIs) |
| `implemented` | bool | REST: always `true`. CLI: `true` once the CLI started and answered its login probe |
| `ready` | bool | this backend could serve a chat now (see states below) |
| `hint` | string | `"ok"` when ready, otherwise the fix, already plain English |
| `default_model` | string | `model_for(id)`: env, then saved model if this is the chosen backend, then built-in default |
| `models`, `models_source`, `models_hint` | list, `"catalog"`\|`"cli"`, string | starting list; see [Model list](#2-model-list) |

REST rows add: `key_present` (bool), `key_source` (`"env"`\|`"file"`\|`null`),
`key_file` (path), `key_mode` (octal string such as `"600"`, or `null`), `base` (upstream
URL). The `anthropic` row also carries `api: "messages"`.

CLI rows add: `kind: "cli"`, `command`, `cli_present` (bool), `cli_path` (string\|null),
`login` (`"ok"` \| `"missing"` \| `"unusable"` \| `"unchecked"`), `probe` (the login
command Nexus runs), `cli_model` (string\|null), `credential: "cli-login"`.
Subscription logins are never read or stored by Nexus; it only runs the vendor CLI.

### Fields added on each row (`ui_api: 1`)

Additive only; existing consumers (`doctor.sh`, the menu bar, `probe.py`,
the current dashboard) read specific keys and are unaffected.

| field | type | value |
| --- | --- | --- |
| `name` | string | display name: `OpenRouter`, `OpenAI`, `Anthropic`, `xAI`, `Nous Portal`, `Codex`, `Claude Code`, `GitHub Copilot`, `Grok Build`, `Antigravity` (the `label` the backend already carries) |
| `kind` | `"rest"` \| `"cli"` | `"cli"` is already present on CLI rows; REST rows gain `"rest"` |
| `state` | enum | `ready` \| `not_configured` \| `cli_missing` \| `not_logged_in` \| `out_of_quota` |

`hint` keeps its existing meaning; the UI shows it verbatim when `state != "ready"`.
`ready` is never changed by the overlays below: it stays what the backend's own check said,
so `probe.py` and `doctor.sh` read the same value as before. Use `state` for the UI verdict.
Only when an overlay changes `state` (a refused key, a usage limit) is `hint` replaced with
the overlay's fix text.

### How each state is detected

| `state` | REST backend | CLI backend |
| --- | --- | --- |
| `ready` | `key_present: true` and, **for the active backend only**, the provider did not reject the key | `cli_present: true` and `login: "ok"` |
| `not_configured` | `key_present: false` (no env var, no key file), **or** the active backend's key was refused (`upstream_status` 401/403 in `/health`) | n/a |
| `cli_missing` | n/a | `cli_present: false`, or the binary was found but could not be started (`login: "unchecked"`) |
| `not_logged_in` | n/a | `cli_present: true` and `login` is `"missing"` or `"unusable"` (the login probe said logged out, or said nothing usable) |
| `out_of_quota` | a recent chat call through this backend hit a usage or rate limit and nothing has succeeded since | same (derived from traffic, below) |

Existing `hint` strings per condition (use them as the fix text):

- REST, no key: `OpenRouter key missing. Set OPENROUTER_API_KEY or create a mode-600 file at <key_file>`
  (`Upstream.readiness`).
- REST, key refused (active backend): `OpenRouter rejected the API key (upstream HTTP 401): <reason>. Replace it and re-check with ./doctor.sh`
  (`credential_rejection`).
- CLI missing: `codex CLI not found. Install the official CLI and sign in there. This bridge does not store login tokens.`
- CLI logged out: `CLI is not logged in. Run `codex login` in a terminal. This bridge does not read or store that login.`
  The login command is per backend: `codex login`, `claude auth login`, `copilot login`,
  `grok login`, `agy`.

`out_of_quota` is **derived from traffic, never probed**. Nexus cannot ask a
Claude/ChatGPT/Grok plan for its balance and this contract does not pretend otherwise. A
backend becomes `out_of_quota` when its most recent **chat call** (a `POST` through `/v1`)
recorded `usage_limit: true` (see [Recent calls](#3-recent-calls)), and it returns to its
normal state on the next successful chat call through it, or after a 15 minute cooldown
(`QUOTA_COOLDOWN_S` in `src/nexus/pulse.py`), whichever is first. A `GET /v1/models` does
not clear it. Only a backend that would otherwise be `ready` is overlaid; a missing key or
login is reported as such. Because a plain HTTP 429 also sets `usage_limit`, a transient
rate limit and an exhausted plan are not distinguishable. The `hint` for this state is
`Usage limit reached on <name>. Wait for the window to reset, or switch backend.` The state
is in memory and clears on restart.

Limits the UI should show rather than hide:

- For a **non-active REST backend**, `ready`/`state: "ready"` means *a key is on file*. The
  Nexus does not call that provider until it is the active backend. Label it "key on
  file", not "verified".
- A rejected key can only be detected on the active backend, and a 401/403 maps to
  `not_configured` (there is no `key_rejected` state; branch on `hint`).
- A client must treat an unknown `state` value as not ready.

### Cached or live

| Signal | Behavior |
| --- | --- |
| REST `key_present`, `key_source`, `key_mode`, `ready` | **Live** on every call. A file/env check only; no network. Key-file edits apply on the next request. An env var change needs a restart. |
| CLI `cli_present`, `login`, `ready`, `models` | **Cached 20 s** per backend and binary (`_PROBE_TTL` in `backends/subscription.py`). A cold call runs the vendor CLI synchronously (up to 15 s per command, one command or two per backend, backends in sequence). Warm polls start no process. |
| `model_ready`, `model_hint`, `upstream_status` (active backend) | Asks the provider, **cached 10 s** (`_MODEL_TTL`), budgeted at `NEXUS_HEALTH_TIMEOUT` (default 2 s). On timeout the answer is `model_ready: null` ("could not confirm"), never a false green. |
| `out_of_quota` | Computed from in-memory traffic on each request. No provider call. |

### Example

Request:

```http
GET /health HTTP/1.1
Host: 127.0.0.1:11435
```

Response (abridged to two rows):

```json
{
  "ok": true,
  "ui_api": 1,
  "service": "nexus",
  "version": "0.7.2",
  "backend": "openrouter",
  "backend_source": "env",
  "backend_known": true,
  "ready": false,
  "model": "google/gemma-4-31b-it:free",
  "model_ready": null,
  "model_hint": "OpenRouter key missing. Set OPENROUTER_API_KEY or create a mode-600 file at /home/<user>/.config/nexus/openrouter.key",
  "bind": "127.0.0.1:11435",
  "loopback_enforced": true,
  "backends": {
    "openrouter": {
      "id": "openrouter",
      "name": "OpenRouter",
      "kind": "rest",
      "state": "not_configured",
      "role": "primary",
      "implemented": true,
      "ready": false,
      "key_present": false,
      "key_source": null,
      "key_file": "/home/<user>/.config/nexus/openrouter.key",
      "key_mode": null,
      "base": "https://openrouter.ai/api/v1",
      "default_model": "google/gemma-4-31b-it:free",
      "hint": "OpenRouter key missing. Set OPENROUTER_API_KEY or create a mode-600 file at /home/<user>/.config/nexus/openrouter.key",
      "models": ["google/gemma-4-31b-it:free", "openai/gpt-4o-mini"],
      "models_source": "catalog",
      "models_hint": "the bridge's own catalog. GET /v1/models lists what OpenRouter really offers for this key."
    },
    "codex": {
      "id": "codex",
      "name": "Codex",
      "kind": "cli",
      "state": "cli_missing",
      "role": "subscription",
      "command": "codex",
      "implemented": false,
      "ready": false,
      "cli_present": false,
      "cli_path": null,
      "login": "unchecked",
      "probe": "login status",
      "default_model": "gpt-5.6-sol",
      "cli_model": null,
      "credential": "cli-login",
      "hint": "codex CLI not found. Install the official CLI and sign in there. This bridge does not store login tokens.",
      "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"],
      "models_source": "catalog",
      "models_hint": "`codex debug models` did not report a model list. These are the bridge's own catalog, not the account's."
    }
  }
}
```

`GET /doctor` returns the same body plus `bridge_key` (`present`, `file`, `mode`; never the
value), `backend_choice_file`, `endpoints`, and `rotate` (every key file path). Use it for a diagnostics screen, not for polling.

## 2. Model list

**`GET /dashboard/models?backend=<id>`**. Source:
`src/nexus/dashboard_models.py` (`models_payload`). `backend` defaults to the active
backend and accepts aliases (`claude`, `grok`, `claude-cli`, ...). An unknown name is
HTTP 404 `{"ok": false, "error": "Unknown backend 'x'."}`.

Response:

| field | meaning |
| --- | --- |
| `backend` | canonical id the list belongs to |
| `source` | `"live"` (the provider listed these for this key), `"cli"` (the CLI reported them), `"catalog"` (Nexus's own starting guess) |
| `hint` | prose caveat; show it |
| `default_model` | what `model_for(backend)` resolves to |
| `models` | `[{"id", "status"}]`, de-duplicated, capped at 400 |
| `truncated` | `true` when more than 400 were dropped |

`status` per row: `live`, `cli`, `catalog`, or `unverified` (Nexus's default model,
which the live list did not contain). A catalog id is not an entitlement.

How it is produced: an API backend **with a key on file** is asked for its real list,
bounded by `NEXUS_HEALTH_TIMEOUT` (2 s). On any failure it falls back to the catalog
(`source: "catalog"`). A CLI backend is never asked; it reuses the cached login probe
(same 20 s cache as `/health`) and so starts no new process on a warm cache. Not cached
server-side for API backends: each request is a live provider call, so fetch it when the
user opens a picker, not on a timer.

### Field added on each row: serving backend

Added with `ui_api: 1`. Each row has `backend` (the id whose list it came from). This is the
same value as the top-level `backend`, repeated so a merged, cross-backend list is
self-describing:

```json
{ "id": "openai/gpt-4o-mini", "status": "live", "backend": "openrouter" }
```

There is deliberately **no** `backend=all` endpoint. To build a combined table the UI
requests each backend whose `state` is `ready` (at most ten requests, one per row) and
tags rows with `backend`. This keeps one code path and avoids a new fan-out endpoint.

### Relationship to `/v1/models`

`GET /v1/models` (bearer, frozen) lists models for **only the active backend** and
is what OpenAI-compatible clients use. Its rows are `{id, object: "model", created,
owned_by}`; a row the upstream did not confirm also carries `warning: "unverified: ..."`
(and the top-level `warning` repeats the first row's). `/dashboard/models` is the
dashboard's richer view of the same data and is the one the UI should use, because it
works for any backend, needs no bearer, and tags confirmation as `status` instead of a
warning string.

The mapping: `status: "live"` and `status: "cli"` are `/v1/models` rows with no
`warning`; `status: "unverified"` and `status: "catalog"` are rows that carry one.

Important for the UI: **`/v1` serves one backend at a time.** `/v1/chat/completions`
always goes to the active backend (`src/nexus/app.py`, `_resolve`); Nexus does not
route a model id to the backend that lists it. A model listed under a non-active backend
is not usable through `/v1` until that backend is made active (section 4). Show that
state; do not imply multi-backend routing.

Example:

```http
GET /dashboard/models?backend=openrouter HTTP/1.1
Host: 127.0.0.1:11435
```

```json
{
  "backend": "openrouter",
  "source": "live",
  "hint": "Listed by the provider for this key. An id marked unverified is the bridge's default and was not in that list.",
  "default_model": "google/gemma-4-31b-it:free",
  "models": [
    { "id": "google/gemma-4-31b-it:free", "status": "live", "backend": "openrouter" },
    { "id": "openai/gpt-4o-mini", "status": "live", "backend": "openrouter" }
  ],
  "truncated": false
}
```

(`backend` inside each row was added with `ui_api: 1`.)

## 3. Recent calls

**`GET /dashboard/pulse`**. Source: `src/nexus/pulse.py`
(`PulseLog`, `PulseMiddleware`). It is the existing record of recent `/v1` calls; there is
no second log.

Retention and limits (these are the existing behavior):

- In-memory ring, **40 entries** (`PULSE_CAP`), newest first. The top-level `cap` field
  says so.
- **Lost on restart.** Nothing is written to disk, by design (no prompts, bodies, headers,
  or keys are kept). A UI that wants more history must keep its own in memory.
- Only `/v1/*` requests are recorded, including `GET /v1/models` and failed auth. Filter on
  `route` (for example `POST /v1/chat/completions`). Control-plane calls and
  `POST /dashboard/bench` are not recorded.
- A model id that looks like credential material is stored as `null`.

### Entry fields that exist

| field | type | notes |
| --- | --- | --- |
| `ts` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `route` | string | `"<METHOD> <path>"`, query stripped |
| `model` | string\|null | the model the request resolved to |
| `status` | int | HTTP status of the response start. A stream that fails after it started still shows `200`. |
| `latency_ms` | int | whole request, measured in the middleware. For a stream it runs until the stream ends, not to the first token |
| `prompt_tokens`, `completion_tokens` | int\|null | only when the upstream body reported usage on a **non-stream** response. Streams report `null`. A subscription CLI that reports no usage may yield `0`, not `null`; treat `0` with a successful call as "unknown". |
| `usage_limit` | bool | `true` on HTTP 429 or a usage-limit error |

Top-level: `cap`, `entries`, and `traffic` (`since`, `calls`, `ok`, `client_error`,
`server_error`, `usage_limit`, `latency_ms` {`p50`, `p95`, `max`, `sample`}, `tokens`
{`prompt`, `completion`, `total`, `known_calls`, `unknown_calls`}, `note`). `traffic` is
process-local since start and is **not** a plan balance.

### Fields added on each entry

Added with `ui_api: 1`. Additive.

| field | type | value |
| --- | --- | --- |
| `backend` | string\|null | id of the backend that handled the call. `null` when the request failed before dispatch (for example a 401 from a bad bridge key) |
| `error_code` | string\|null | `GatewayError.code` on a failure (`codex_usage_limit`, `openrouter_upstream_error`, `invalid_api_key`, ...), else `null` |
| `hint` | string\|null | the same redacted `hint` the client received in the error body, else `null` |

Mechanism: the same per-request context variables the existing `note_usage_limit()` uses
(`note_backend`, `note_error` in `src/nexus/pulse.py`), set from the `/v1` handlers and
`handle_gateway_error` in `app.py`. `error_code` and `hint` are never taken from an
upstream body: `error_code` must look like `[a-z0-9_-]{1,64}` (backend ids such as `claude-code` put a hyphen in codes like `claude-code_usage_limit`) or it is stored as `null`, and
`hint` is redacted and cut at 400 characters. Both are `null` on a success.
`backend` is also set for `GET /v1/models` (which backend answered), and on a failure after
dispatch started; a stream that fails after its first byte still reports `status: 200`
and no `error_code`.

### Example

Request:

```http
GET /dashboard/pulse HTTP/1.1
Host: 127.0.0.1:11435
```

Response:

```json
{
  "cap": 40,
  "entries": [
    {
      "ts": "2026-10-08T07:41:09Z",
      "route": "POST /v1/chat/completions",
      "model": "gpt-5.6-sol",
      "status": 429,
      "latency_ms": 2210,
      "prompt_tokens": null,
      "completion_tokens": null,
      "usage_limit": true,
      "backend": "codex",
      "error_code": "codex_usage_limit",
      "hint": "The upstream is rate limiting or out of quota. Wait and retry, or GET /doctor."
    },
    {
      "ts": "2026-10-08T07:40:12Z",
      "route": "POST /v1/chat/completions",
      "model": "openai/gpt-4o-mini",
      "status": 200,
      "latency_ms": 1840,
      "prompt_tokens": 24,
      "completion_tokens": 96,
      "usage_limit": false,
      "backend": "openrouter",
      "error_code": null,
      "hint": null
    }
  ],
  "traffic": {
    "since": "2026-10-08T07:36:37Z",
    "calls": 2,
    "ok": 1,
    "client_error": 1,
    "server_error": 0,
    "usage_limit": 1,
    "latency_ms": { "p50": 2025, "p95": 2191, "max": 2210, "sample": 2 },
    "tokens": { "prompt": 24, "completion": 96, "total": 120, "known_calls": 1, "unknown_calls": 1 },
    "tool_repair": { "repaired": 0, "rejected": 0 },
    "note": "Traffic through this door since the process started. Not your Claude, ChatGPT, or Grok plan balance."
  }
}
```

(`backend`, `error_code`, `hint` were added with `ui_api: 1`. `traffic.tool_repair` counts
tool-call answers Nexus repaired or rejected as `invalid_tool_call` since the process
started; it is additive.)

## 4. Config

The "config" is what the existing setup flow already writes. No new storage.

| What | Where | Mode | Secret |
| --- | --- | --- | --- |
| Active backend id | file at `NEXUS_BACKEND_FILE` (default `<repo>/.nexus_backend`), else `NEXUS_BACKEND` env | `0644` | no |
| Default model | `NEXUS_MODEL` env, else the file at `NEXUS_MODEL_FILE` (default `<repo>/.nexus_model`) | `0644` | no |
| Provider key | env var (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, `NOUS_API_KEY`), else a key file | **`0600`** | **yes** |
| Bridge key | `NEXUS_KEY_FILE`, default `<repo>/.nexus_api_key` | **`0600`** | **yes** |

Provider **keys** still prefer env, then file, then absent (`src/nexus/keys.py`).
A key written from the UI is ignored while the env var is set: the UI must show
`key_source` and warn when it is `"env"`.

The **active backend** is the opposite: the setup/Settings choice file wins over
`NEXUS_BACKEND`. The env var is only the default when nothing is saved
(`src/nexus/config.py`). A packaged LaunchAgent still sets `NEXUS_BACKEND`
(the value at `./nexus start`); that pin does not hide a later Settings save,
and it does not hide the saved file after a relaunch. Deleting the choice file
makes the env pin the backend again.

Key file path pattern (`config._platform_key`, per-backend override env
`NEXUS_<NAME>_KEY_FILE`):

- macOS: `~/Library/Application Support/Nexus/.<name>-os3.key`
- elsewhere: `${XDG_CONFIG_HOME:-~/.config}/nexus/<name>.key`
- installs made under the old names: see [upgrading-from-os3-bridge.md](upgrading-from-os3-bridge.md)

where `<name>` is `openrouter`, `openai`, `anthropic`, `xai`, or `nous`. The file holds
one line. It is written atomically (a `0600` temp file in the same directory, then
rename, with `O_NOFOLLOW`) and re-read per request, so a rotated key takes effect with no
restart. The same path is in `GET /health` as `key_file` and `key_mode`. A subscription
CLI backend has no key file; its login lives in the vendor CLI.

Keys are **never echoed back** by any endpoint. Presence and source are the most any GET
reveals; `masked` (section 4.1) reveals at most the last four characters.

Applying a change: the backend choice file is read on every request, so a Settings
save is the next request's backend without a restart (in-flight requests finish on
the previous backend). `NEXUS_MODEL` and provider key env vars still need a
process restart. The write response `restart` field says the new backend is already
serving; `restart_command` is empty. `GET /health` still carries `restart_*` about
whether this process is a packaged unit (the page cannot restart itself).

### 4.1 `GET /dashboard/config`

Added with `ui_api: 1`. Before it, `GET /dashboard/config` answered
`405 {"detail": "Method Not Allowed"}`; detect an old Nexus by that 405 or a missing
`ui_api`. Source: `src/nexus/dashboard_config.py`. Sent with `Cache-Control: no-store`.

Response:

| field | type | meaning |
| --- | --- | --- |
| `ok` | `true` | |
| `backend` | string | active backend id |
| `backend_source` | `"env"` \| `"file"` \| `"default"` | where it came from |
| `model` | string | effective default model |
| `model_source` | `"env"` \| `"file"` \| `"default"` | where it came from |
| `choice_file`, `model_file` | string | the two choice-file paths |
| `keys` | object | one entry per REST backend: `openrouter`, `openai`, `anthropic`, `xai`, `nous` |
| `keys.<id>.present` | bool | same value as `key_present` in `/health` |
| `keys.<id>.source` | `"env"` \| `"file"` \| `null` | same value as `key_source` |
| `keys.<id>.file` | string | same value as `key_file` |
| `keys.<id>.mode` | string\|null | same value as `key_mode`; anything other than `"600"` should be flagged |
| `keys.<id>.masked` | string\|null | `null` when absent; else `"••••"` plus the last four characters when the key is at least 20 characters, otherwise just `"••••"` |
| `restart_available`, `restart_managed`, `restart_command`, `restart_note` | | same as `/health` |

`keys.*.present/source/file/mode` repeat what `/health` already reports so the config
screen is one request; they are computed by the same functions, not a second source of truth.

Example:

```http
GET /dashboard/config HTTP/1.1
Host: 127.0.0.1:11435
```

```json
{
  "ok": true,
  "backend": "openrouter",
  "backend_source": "file",
  "model": "openai/gpt-4o-mini",
  "model_source": "file",
  "choice_file": "/opt/nexus/.nexus_backend",
  "model_file": "/opt/nexus/.nexus_model",
  "keys": {
    "openrouter": {
      "present": true,
      "source": "file",
      "file": "/home/<user>/.config/nexus/openrouter.key",
      "mode": "600",
      "masked": "••••c2f9"
    },
    "openai": { "present": false, "source": null, "file": "/home/<user>/.config/nexus/openai.key", "mode": null, "masked": null }
  },
  "restart_available": false,
  "restart_managed": false,
  "restart_command": "",
  "restart_note": "This is a foreground process, not a launchd or systemd unit, so there is no managed service to restart from here. A backend or model saved on this page is used on the next request. Stop this process and start it again to reload environment variables."
}
```

### 4.2 `POST /dashboard/config`

Source: `src/nexus/app.py` (`dashboard_config`) calling
`src/nexus/setup_flow.py` (`apply_dashboard_choice`), the same path as
`./nexus setup`. Same-origin JSON only (see [Auth](#6-auth-and-the-same-origin-guard)).
This is how both "set default backend/model" and "write a key" are done; there is no
separate key endpoint.

Request body:

| field | type | required | meaning |
| --- | --- | --- | --- |
| `backend` | string | yes | any backend id or alias; becomes the active backend |
| `api_key` | string | no | a provider key, REST backends only. One token, at most 4096 characters |
| `model` | string | no | one id of letters, numbers and `. _ : / -`, at most 128 characters |

Behavior the UI must account for:

- A typed `api_key` is checked locally (no spaces, not a placeholder) and then **verified
  against the provider** with an authenticated call **before** anything is written. This is
  a blocking network call (it runs in a worker thread); expect up to a few seconds. A key
  the provider refuses (401/403) is a 400 and nothing is written.
- With no `api_key`, an API backend must already have a key file or the call is a 400.
  A key already present in the env is **not** counted; only a key file is.
- **`model` omitted or blank clears the saved model** (it reverts to the backend's
  built-in default). It does not mean "leave unchanged". Always send the current model
  back, or you will reset it.
- The backend choice is written every time. Saving a key for backend B therefore also makes
  B the active backend (see `activate`).
- A CLI backend needs its binary on `PATH` and takes no key (a non-empty `api_key` is a 400).
  Its login stays in the vendor CLI.
- Activating a backend that is not ready (the same `/health` row Settings uses:
  CLI not logged in, key not present) is **409** `not_ready`. The body `error` is
  that row's hint and `hint` is the next step. The choice file and the model file
  are not written, so Nexus keeps serving the previous backend. `activate: false`
  does not run this check.
- Changing the backend resets the saved model to the new backend's default unless
  the saved id (or a posted `model`) is one that backend lists. `model_note` says
  which. A model id does not carry from one backend to another.
- The key is written `0600` atomically; the response never contains it.

Response `200`:

| field | meaning |
| --- | --- |
| `ok` | `true` |
| `backend` | canonical id now chosen |
| `choice_file`, `model_file` | paths written |
| `model` | the saved model, `""` when cleared |
| `key_written` | `"yes"` or `"no"` (a string, not a bool) |
| `key_file` | the key file path for an API backend, `""` for a CLI |
| `model_note` | one-line outcome of the model write, including a warning when `NEXUS_MODEL` still wins. On a backend change it says the model was kept or reset |
| `warning` | present only when the switch to Nous could not copy the Hermes token. Plain reason, never the token |
| `restart`, `restart_available`, `restart_command`, `restart_managed` | restart advice. `restart_available` and `restart_managed` are the strings `"yes"`/`"no"` here (they are booleans on `/health`) |

Errors: `400` `{"ok": false, "error": "<message>"}` for a bad body, unknown backend,
missing/invalid/refused key, missing CLI, or bad model id; `403` from the guard.

### Fields added on `POST /dashboard/config`

Added with `ui_api: 1`.

- Request `activate` (bool, default `true`; any other type is `400 invalid_request`): when
  `false`, write the key (and nothing else) without changing the active backend or
  touching the saved model. This lets the UI add a second provider's key without
  switching. With `activate: false`, `model` is ignored, only API backends are accepted
  (a CLI is `400 cli_has_no_key`), and an `api_key` is required (`400 key_missing`). The
  key is still verified against the provider first and written `0600`.
- Response `activated`: `"yes"` or `"no"` (a string, like `key_written`). With
  `activate: false`, `backend` is the backend that is **still** active (not the one whose
  key was written; the UI knows that from its request), `model` is the unchanged saved
  model, `key_file` is the written file, and `model_note` is
  `Backend and model unchanged.`
- Errors carry `code` and `hint` per the [envelope](#error-envelope).

Example (change model, keep the same backend):

```http
POST /dashboard/config HTTP/1.1
Host: 127.0.0.1:11435
Origin: http://127.0.0.1:11435
Content-Type: application/json

{ "backend": "openrouter", "model": "openai/gpt-4o-mini" }
```

```json
{
  "ok": true,
  "backend": "openrouter",
  "choice_file": "/opt/nexus/.nexus_backend",
  "model": "openai/gpt-4o-mini",
  "model_file": "/opt/nexus/.nexus_model",
  "key_written": "no",
  "key_file": "/home/<user>/.config/nexus/openrouter.key",
  "model_note": "Model saved. The next request uses it when the client does not name one.",
  "restart": "The door is serving this backend now. Requests already in flight finish on the previous one.",
  "restart_available": "no",
  "restart_command": "",
  "restart_managed": "no",
  "activated": "yes"
}
```

Example (write a key):

```json
{ "backend": "openai", "api_key": "<paste provider key here>", "model": "gpt-4o-mini" }
```

responds with the same shape and `"key_written": "yes"`. A refused key:

```json
{ "ok": false, "error": "OpenAI rejected the key (HTTP 401). The key is wrong or expired; fix it and run setup again." }
```

(With the envelope this also carries `"code": "key_rejected"` and a `hint`. The
`error` text is built in `setup_flow._check_candidate_key` from `verify_provider_key`; the
provider label and status vary. An unreachable provider reads `could not reach <label> to
check the key (<ExceptionName>). The <label> key could not be verified; check connectivity and
run setup again.`)

## 5. Bench run

**`POST /dashboard/bench`**. Added with `ui_api: 1`. Source: `src/nexus/bench.py`.

Purpose: time the same prompt across chosen backend/model pairs. It calls each backend
**in-process** (`build_backend(<id>).open_chat`, non-streaming), so it works for backends
that are not the active one, needs no bridge key, and is subject to the same
`NEXUS_MAX_INFLIGHT` limit as chat. It does not go through `/v1`, so bench calls are
not in `/dashboard/pulse` and do not trip `out_of_quota`.

It spends real quota or money on the chosen backends. The UI must make the user start it
explicitly and must never run it on a timer.

Request (same-origin JSON):

| field | type | required | default | limits |
| --- | --- | --- | --- | --- |
| `targets` | `[{"backend", "model"}]` | yes | | 1 to 6 items. `backend` is an id or alias and may be omitted for the active backend. `model` may be omitted for that backend's `default_model`. Must pass the model-id rule in section 4.2 |
| `prompt` | string | yes | | 1 to 2000 characters |
| `max_tokens` | int | no | 64 | 1 to 256. Sent in the request to REST backends. **Not applied to CLI targets**: none of the five subscription CLIs is started with a max-tokens option, so a CLI target is bounded by `timeout_s`, not by tokens (its `output_tokens` can exceed `max_tokens`) |
| `timeout_s` | number | no | `min(60, upstream timeout)` | 1 to 60, per target |

**Sync, not async.** The request returns when every target is done. Targets run one after
another, in the order given, so they do not compete for bandwidth or a CLI's rate limit.
Worst case is `len(targets) * timeout_s` (6 × 60 s), so the UI must not apply a short
fetch timeout, should show per-target progress as "pending until the response", and may
abort with `AbortController`: when the client disconnects the server finishes the target in
flight, skips the rest, and releases the one-at-a-time lock. One bench at a time: a second concurrent request is `409 bench_busy`. An async
variant (`202` with a `run_id`, then `GET` to poll) is deliberately not specified: the
row shape below is the same either way, so it can be added later without breaking this.

When a target times out or the run is cancelled, Nexus stops the CLI it started: it
sends SIGTERM to the CLI's whole process group (the CLI and anything it spawned), waits
half a second, sends SIGKILL to what is left, and reaps it, before the lock is released.
The same cleanup now covers a `/v1` chat call on a CLI backend whose caller goes away.

A target that fails does **not** fail the request. The response is `200` and that row has
`ok: false`. Request-level problems (bad body, over limits, unknown backend, guard) are
`4xx` with the [envelope](#error-envelope). Target order is the request order. A target
without `backend` uses the active backend; one without `model` uses
`default_model` for its backend (`model_for`).

Response `200` (sent with `Cache-Control: no-store`):

| field | type | meaning |
| --- | --- | --- |
| `ok` | `true` | the request was accepted and ran |
| `started_at` | string | UTC `YYYY-MM-DDTHH:MM:SSZ` |
| `rows` | array | one per target, same order as `targets` |

Row:

| field | type | meaning |
| --- | --- | --- |
| `backend` | string | canonical id |
| `kind` | `"rest"` \| `"cli"` | |
| `model` | string | model sent |
| `ok` | bool | upstream answered 200 with assistant text |
| `total_ms` | int | wall time for the whole call, including CLI startup for CLIs |
| `input_tokens` | int\|null | from the upstream `usage` when reported and greater than 0 (a CLI that reports `0` is "unknown", so `null`) |
| `output_tokens` | int\|null | from the upstream `usage` when reported and greater than 0 |
| `tok_per_s` | number\|null | `output_tokens / (total_ms / 1000)`, one decimal. **Always `null` for CLI backends**, and `null` when `output_tokens` is null or the call failed. This is end-to-end throughput including time to first token, not decode speed |
| `error` | object\|null | `null` when `ok`; else `{"code", "hint"}`: the `GatewayError` code and its redacted hint (for example `codex_login_required`, `openrouter_upstream_error`), or one of the bench's own codes below |

Bench row error codes that are not a `GatewayError` code: `backend_not_ready` (not called),
`bench_timeout` (no answer within `timeout_s`; `total_ms` is the time waited), `bench_empty`
(the backend answered without assistant text), `internal_error` (an unexpected failure on
that target; the hint is generic).

A target whose backend is not `ready` is not called: its row is `ok: false` with
`error.code: "backend_not_ready"` and the backend's own `hint`. The response never
contains the prompt, the completion text, or a key.

Example:

```http
POST /dashboard/bench HTTP/1.1
Host: 127.0.0.1:11435
Origin: http://127.0.0.1:11435
Content-Type: application/json

{
  "targets": [
    { "backend": "openrouter", "model": "openai/gpt-4o-mini" },
    { "backend": "codex", "model": "gpt-5.6-sol" },
    { "backend": "openai" }
  ],
  "prompt": "Reply with exactly: ok",
  "max_tokens": 32
}
```

```json
{
  "ok": true,
  "started_at": "2026-10-08T07:45:00Z",
  "rows": [
    {
      "backend": "openrouter",
      "kind": "rest",
      "model": "openai/gpt-4o-mini",
      "ok": true,
      "total_ms": 912,
      "input_tokens": 14,
      "output_tokens": 2,
      "tok_per_s": 2.2,
      "error": null
    },
    {
      "backend": "codex",
      "kind": "cli",
      "model": "gpt-5.6-sol",
      "ok": true,
      "total_ms": 6340,
      "input_tokens": null,
      "output_tokens": null,
      "tok_per_s": null,
      "error": null
    },
    {
      "backend": "openai",
      "kind": "rest",
      "model": "gpt-4o-mini",
      "ok": false,
      "total_ms": 0,
      "input_tokens": null,
      "output_tokens": null,
      "tok_per_s": null,
      "error": {
        "code": "backend_not_ready",
        "hint": "OpenAI key missing. Set OPENAI_API_KEY or create a mode-600 file at /home/<user>/.config/nexus/openai.key"
      }
    }
  ]
}
```

## 6. Auth and the same-origin guard

Sources: `src/nexus/guard.py` (`refusal`, `check_request`, `LoopbackGuard`),
`src/nexus/auth.py` (`require_bearer`, `ensure_bridge_key`), `src/nexus/app.py`.

### What needs what

| Routes | Host check | Same-origin write check | Bridge bearer |
| --- | --- | --- | --- |
| `/v1/*` | no | no | **yes** (`Authorization: Bearer <bridge key>`) |
| Everything else: `/`, `/health`, `/doctor`, `/dashboard/*` | **yes** | **yes**, on non-`GET`/`HEAD`/`OPTIONS` | no |

The Nexus key is **only** for `/v1`. Every endpoint in this document except the `/v1`
rows is unauthenticated by design; the loopback bind, the guard, and the absence of CORS
are what protect it. The UI never needs the Nexus key for config, models, status,
calls, or bench.

### The guard

Every non-`/v1` request is refused with `403 {"ok": false, "error": "<reason>"}` unless:

1. **`Host`** (name only, port ignored) is `127.0.0.1`, `localhost`, or `[::1]`.
   This defeats DNS rebinding (a hostile name resolving to `127.0.0.1` arrives with a
   foreign `Host`).
2. For a **write** (anything other than `GET`, `HEAD`, `OPTIONS`) also:
   - `Sec-Fetch-Site`, if sent, is `same-origin` or `none` (browsers send it; `curl` does not);
   - `Origin`, if sent, has the same host and port as `Host` (a `null` or foreign origin is refused);
   - `Content-Type` is `application/json`. A cross-origin page cannot send that without a
     CORS preflight, and **Nexus never grants CORS** (there is no CORS middleware and
     no `Access-Control-Allow-*` header anywhere), so the browser blocks it.

Reasons, in order of check: `Host must be 127.0.0.1 or localhost.`, `Cross-site writes are
refused.`, `Origin does not match this door.`, `Writes must be sent as application/json.`

`GET` requests are only Host-checked. A foreign page can make the browser *send* a `GET`
to `127.0.0.1:11435`, but without CORS headers the browser will not let that page *read*
the response. The Nexus key is protected by that, not by an Origin check on `GET`.

### How the UI gets and uses the Nexus key

Because the UI is served by Nexus at `/`, it is same-origin and passes the guard with
no configuration. **The UI should not need the Nexus key at all.** The control plane and
bench are server-side.

If a screen genuinely has to call `/v1` from the browser (for example a chat playground):

- Fetch it once with `GET /dashboard/bridge-key`.
  - `{"present": false}` when no key file exists. The endpoint **never creates one**
    (Nexus mints it at startup).
  - `{"present": true, "key": "os3_..."}` otherwise. Sent with `Cache-Control: no-store`.
- Hold it in memory only. Do not put it in `localStorage`, `sessionStorage`, cookies, a
  URL, `console`, or rendered DOM text. Fetch it on demand (as the existing Copy button
  does, on click) and send it as `Authorization: Bearer <key>` to `/v1/*` only.
- It is never exposed cross-origin: `/dashboard/bridge-key` is guarded by Host and has no
  CORS headers, so a page from another origin, or a rebound hostname, cannot read it.
  It also requires browser provenance (`Sec-Fetch-Site: same-origin`, or a loopback
  `Origin` on Nexus's port): `curl` and scripts get 403 `provenance_required`.
  Anything already running as the same user on this machine can read the key file directly
  and is outside this model.

Example:

```http
GET /dashboard/bridge-key HTTP/1.1
Host: 127.0.0.1:11435
```

```json
{ "present": true, "key": "os3_<redacted>" }
```

A `/v1` call without the right key returns `401` in the `/v1` envelope, for example
`code: "missing_api_key"` or `"invalid_api_key"`, with a `hint`.

### Examples of guard refusals

```http
GET /health HTTP/1.1
Host: evil.example
```

```json
{
  "ok": false,
  "error": "Host must be 127.0.0.1 or localhost.",
  "code": "host_refused",
  "hint": "Open the page at http://127.0.0.1:11435/, not through another hostname."
}
```

```http
POST /dashboard/config HTTP/1.1
Host: 127.0.0.1:11435
Origin: http://evil.example
Content-Type: application/json
```

```json
{
  "ok": false,
  "error": "Origin does not match this door.",
  "code": "origin_mismatch",
  "hint": "Send this request from the page the bridge serves at /, not from another origin."
}
```

### Developing the UI

Do **not** loosen the guard or add CORS to make a dev server work. `vite dev` on another
port is a different origin, so its writes are refused (`Origin` mismatch) and its reads are
unreadable. Use a dev proxy that forwards to `127.0.0.1:11435` and rewrites `Host` and
`Origin` to that address, or build the static bundle and let Nexus serve it at `/`
(next section).

## 7. Serving the UI

Added with `ui_api: 1`. Source: `src/nexus/ui_static.py`, `src/nexus/config.py`
(`ui_dist_dir`).

If `<dist>/index.html` exists, Nexus serves the built UI at `/` from the same origin
as the API. `<dist>` is `$OS3_UI_DIST` when set, else `ui/dist` under the checkout. It is
checked on every request, so a UI built while Nexus runs is picked up without a
restart. If there is no `index.html` there, nothing changes: `GET /` is the built-in
dashboard, and an unknown path is the framework's `{"detail": "Not Found"}` 404.

With a built UI:

- `GET`/`HEAD /` returns `index.html`. `GET`/`HEAD <path>` returns that file from `<dist>`
  when it exists (content type from the extension).
- A path with **no file extension** that is not a file (a client-side route such as
  `/models` or `/backends/codex`) returns `index.html` with `200`: single-page-app
  fallback. A path **with** an extension that is not a file (`/assets/missing.js`) is a
  `404`, so a stale hashed asset fails loudly instead of loading the app shell.
- These are never served from `<dist>`, and their behavior is unchanged: `/v1` and
  `/v1/*`, `/dashboard` and `/dashboard/*` (including `/dashboard/fonts/*`), `/health`,
  `/doctor`. Only `GET` and `HEAD` reach the UI; every other method is handled as before.
  So the UI must not claim `/v1`, `/dashboard/*`, `/health`, or `/doctor` for its own
  client routes.
- Responses carry `Cache-Control: no-cache` and `X-Content-Type-Options: nosniff`.
  Revalidation is cheap on loopback and avoids a stale shell after a rebuild.
- The same-origin guard runs first. A foreign `Host` gets the `host_refused` 403 for the
  shell and for every asset. Nothing outside `<dist>` is readable: the resolved path must
  stay inside it, so `..` segments and symlinks that point out of it are not followed.
- A UI served this way is same-origin with every endpoint above, so it passes the guard
  with no configuration and needs no CORS.

`/v1` bytes are identical with and without a UI (`tests/test_v1_unchanged.py`,
`tests/test_ui_static.py`).

## 8. Rabbit OS3 setup

Additive: `ui_api` stays `1`, so feature-detect with a `404`/`405` on
`GET /dashboard/rabbit` (an older Nexus). Sources: `src/nexus/dashboard_rabbit.py` over
the engine in `src/nexus/rabbit_setup.py`; tests in `tests/test_rabbit_dashboard.py`. The
CLI (`./nexus rabbit ...`) returns the same data; see the README, "Set up Rabbit OS3".

This registers Nexus as a **Local** connection in the user's Rabbit OS3 account so they
do not paste a URL, key and model by hand. It drives a headed Chrome on a dedicated, already
signed-in profile and calls Rabbit's **undocumented** web API, so it can break whenever
Rabbit changes its site. Show it as optional and say so.

The guard in [section 6](#6-auth-and-the-same-origin-guard) applies unchanged: loopback
`Host`, same-origin `application/json` writes, no bearer token. Writes are additionally
refused unless the user set `NEXUS_RABBIT_SETUP=on` (the process environment; the UI
cannot turn it on). **No response contains the Nexus key**, only a masked form
(`****` plus its last four characters) and the *path* of the key file.

### 8.1 `GET /dashboard/rabbit`

Read-only and always allowed. Opens no browser and never talks to Rabbit. Optional query
`?model=<id>` plans for that model instead of the default. `Cache-Control: no-store`.

```json
{
  "ok": true,
  "opt_in": false,
  "plan": {
    "ok": true,
    "opt_in": false,
    "opt_in_env": "NEXUS_RABBIT_SETUP",
    "base_url": "http://127.0.0.1:11435/v1",
    "key_file": "/srv/nexus/.nexus_api_key",
    "key_present": true,
    "context_window": 65536,
    "model": "meituan/longcat-2.5-preview:free",
    "model_source": "fallback",
    "billing": "free",
    "warnings": [],
    "problems": [],
    "models": [
      {"id": "meituan/longcat-2.5-preview:free", "billing": "free", "unverified": false},
      {"id": "openai/gpt-4o-mini", "billing": "unknown", "unverified": false}
    ],
    "models_truncated": false,
    "automatic": {"possible": true, "blocked_by": []},
    "manual": {
      "base_url": "http://127.0.0.1:11435/v1",
      "key_file": "/srv/nexus/.nexus_api_key",
      "model_id": "meituan/longcat-2.5-preview:free",
      "context_window": 65536
    },
    "settings_url": "https://os3.rabbit.tech/settings/api-keys"
  },
  "status": {
    "opt_in": false,
    "opt_in_env": "NEXUS_RABBIT_SETUP",
    "cdp": {"host": "127.0.0.1", "port": 9333, "profile_dir": "/home/<user>/.config/nexus/os3-chrome-debug", "profile_exists": false, "chrome_found": true, "listening": false},
    "node_id": {"present": true, "path": "/home/<user>/.rabbit-agent/connection.json"},
    "key": {"path": "/srv/nexus/.nexus_api_key", "present": true},
    "backup": {"path": "/srv/nexus/.nexus_rabbit_backup.json", "present": false, "saved_at": null, "previous_provider": null},
    "last_apply": null,
    "settings_url": "https://os3.rabbit.tech/settings/api-keys"
  }
}
```

- `plan.model` is `null` and `plan.ok` is `false` when no model could be chosen or the
  requested one is unknown; `plan.problems[]` is `{code, message, hint}` (`closest` too, for
  `unknown_model`). `plan.warnings` carries the billing warning.
- `billing` is `"free"` only for ids ending `:free`; anything else is `"unknown"` (Nexus
  has no price list), which the UI should present as "may be billed", not as "paid".
  `models[].unverified` means the list shows the id but the upstream did not confirm it.
- `plan.models` is capped at 400 rows (`models_truncated`).
- `plan.restore_key` is `{source: "env", path}` when `NEXUS_RABBIT_RESTORE_KEY_FILE` is
  set, else `{source: "auto", candidates, skipped_unreadable, dirs}`: the plan cannot know
  OS3's masked key, so it only counts the files that could match. `dirs` is the
  `NEXUS_RABBIT_KEY_DIRS` entries plus the `rabbit-keys/` folder next to the Nexus key;
  Nexus's own key file is never counted or matched.
- `status.cdp.listening` is `null` when it could not be checked. `status.last_apply` is
  `{at, model, endpoint, context_window, masked}` after a successful setup, else `null`.

### 8.2 `POST /dashboard/rabbit/apply`

Request: `{"model": "<id>", "dry_run": true, "accept_no_rollback": false}`. `model` is
optional text (default: the plan's model); `dry_run` and `accept_no_rollback` are optional
booleans, default `false`; anything that is not `true` or `false` is `400 invalid_request`.
The model is matched exactly, or case-insensitively when that finds exactly one id; it is
never guessed.

`accept_no_rollback`: apply deletes OS3's current connection before it registers the new one,
and puts the old one back if that fails. OS3 only registers a Local model that Nexus
lists in `/v1/models`. When OS3's current Local connection to Nexus uses a model that is
not in that list (for example `stealth/space-bunny-alpha`), the put-back would be refused and
OS3 could be left with no provider. Apply therefore refuses before deleting anything
(`409 rollback_unavailable`, see below) unless `accept_no_rollback` is `true`. A dry run only
warns about it.

A dry run reads OS3's current connection and returns the diff without changing OS3 or
writing any file:

```json
{
  "ok": true,
  "action": "apply",
  "mode": "auto",
  "dry_run": true,
  "changed": false,
  "model": "openai/gpt-4o-mini",
  "billing": "unknown",
  "warnings": [
    "openai/gpt-4o-mini is not marked free (no ':free' suffix); it may be billed by the provider or count against your plan.",
    "OS3 currently uses openrouter; that connection is replaced. Set NEXUS_RABBIT_RESTORE_KEY_FILE if you want it restorable."
  ],
  "diff": {
    "before": {"provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1", "modelId": "anthropic/claude-sonnet-4.5", "masked": "sk-or-v1-****wxyz"},
    "after": {"provider": "local", "endpoint": "http://127.0.0.1:11435/v1", "modelId": "openai/gpt-4o-mini", "contextWindow": 65536, "key": "****c1d7"}
  }
}
```

When `diff.before` is not a Local connection, the reply also has `restore_key`:
`{source, path}` with `source` one of `"env"`, `"auto-detected"` (the one key file that fits
OS3's masked key), `"ambiguous"` (several do, so none is used; `paths` lists them) or `"none"`.
Optional fields: `skipped_unreadable` lists files left unread because they are not owner-only;
`skipped_not_one_key` lists files that do not hold exactly one key on one line;
`mask_too_short: true` means OS3's mask shows fewer than 3 characters before the `****` or 4
after it, so nothing was auto-picked; `problem` (with `source: "env"`) says why the named file
cannot be used. It holds paths only, never a key.

`diff.before` is `null` when OS3 has no connection. A real run (`dry_run: false`) returns the
same shape with `dry_run: false`, `changed: true`, `backup: {path, kept_existing}` and
`verified` (`{provider, endpoint, modelId, masked}` as OS3 reads it back). When OS3 already
points at Nexus with that model, `changed` is `false` and `note` says so.

Outcomes, and how to tell them apart:

| Case | HTTP | Body |
| --- | --- | --- |
| Done, or a dry run | 200 | `ok: true` as above |
| Automatic path unavailable (no Chrome, signed out, CloudFront block, no node id) | 200 | `ok: false`, `mode: "manual"`, `code` (`chrome_unavailable`, `cdp_unavailable`, `signed_out`, `blocked`, `rabbit_agent_missing`, ...), `error`, `hint`, `url`, `manual: {base_url, key_file, model_id, context_window}`. Show `manual` and a link to `url`; the key is a **file path**, never a value |
| Failed after the old connection was removed | 502 | `ok: false`, `code: "restore_attempted"` (the previous connection was put back and read back; `restore.outcome: "restored"`) or `code: "restore_failed"` (it could **not** be, show this loudly), plus `cause: {code, message}` and `restore: {outcome, detail}`. Other failure codes: `delete_failed` (`restore.outcome: "unchanged"`), `register_failed`, `verify_failed` |
| Refused before anything changed | 403 / 400 / 409 | the standard error envelope: `opt_in_required` (403), `unknown_model` (400, plus `closest: [ids]`), `model_required`, `models_unavailable`, `bridge_key_missing`, `invalid_config` (400/409), `invalid_request` (400, a field has the wrong type), `rabbit_busy` (409) |
| Refused because a failed change could not be rolled back | 409 | the envelope with `code: "rollback_unavailable"`, a plain `error` naming the model, a `hint`, `url`, `manual: {base_url, key_file, model_id, context_window}` (the same paste-in values as the manual fallback) and `diff`. Nothing was deleted or written. Show `manual`, and offer to resend with `accept_no_rollback: true` |
| Done, but the read-back could not be checked | 200 | `ok: true`, `changed: true`, `verify: "unconfirmed"`, no `verified`, and a `warnings` entry starting `verify_unconfirmed`. OS3 accepted the registration; run `./nexus rabbit verify` or re-read the plan |

One change runs at a time (`409 rabbit_busy`). A run can take tens of seconds when Chrome has
to start; do not poll it faster than the UI polls `/health`.

### 8.3 `POST /dashboard/rabbit/restore`

Request: `{}`. Puts back the connection saved before the last setup (a Local connection on
Nexus, or another provider whose key file is named in
`NEXUS_RABBIT_RESTORE_KEY_FILE`). Same outcome table as 8.2, with `action: "restore"`
and `diff` as `{before: <current>, after: <saved>}`. If the saved connection cannot be put
back (for example another provider with no key file), nothing is deleted and the answer is
`502 restore_failed` with `changed: false`. `409 no_backup` when there is nothing saved.

Restore checks before it deletes anything. If the saved connection is Local and OS3 would
refuse to register it (its model is not in Nexus's live `/v1/models`, the model list
cannot be read, or there is no node id), the answer is `502` with `changed: false` and
`code: "restore_model_unavailable"` or `"restore_node_missing"`, and OS3 keeps its current
connection. If the saved connection still fails to register after the delete, the current
Local connection is registered again and the `restore_failed` answer carries
`current_put_back` (`true`, `false`, or `null` when nothing had been deleted); `false` means
OS3 may have no provider.

## Versioning and stability

- The Nexus version is `GET /health` → `version` (currently `0.7.2`, from
  `src/nexus/__init__.py`). Control-plane routes are unversioned (`/dashboard/*`, no
  `/v1`-style prefix); the `/v1` prefix belongs to the OpenAI surface only.
- **Frozen:** `/v1/*`, the bind (`127.0.0.1:11435`), and the existing response fields
  listed above. A field documented here is not renamed or removed within a minor
  version; changes to one are called out in `CHANGELOG.md`.
- **Additive only:** new fields may appear anywhere. The UI must ignore unknown fields and
  treat unknown enum values (`state`, `status`, `source`) as "not ready" or "unknown".
- **Feature detection:** `GET /health` and `GET /doctor` carry `ui_api`, an integer that
  bumps only on a breaking change to this contract. This document describes `ui_api: 1`.
  A Nexus without it predates the items marked "added with `ui_api: 1`": there,
  `backends.<id>.state`, `entries[0].backend`, `GET /dashboard/config`, `POST
  /dashboard/bench`, and `activate` are absent, and an endpoint that answers `404` or `405`
  is "not available yet", not an error to show. Derive what you can from the older fields
  as described in each section.
- Process-local data (`pulse`, `traffic`) resets when the process restarts; a `since`
  value that moves forward means Nexus restarted.

## Fallback fields (additive)

When `NEXUS_FALLBACK` or `.nexus_fallback.json` names a chain, a chat request that
hits a usage limit is retried on the next backend (see `CONNECT.md`). `GET /health`,
`GET /doctor` and `GET /dashboard/pulse` carry a `fallback` object, and `GET /dashboard/pulse`
adds `traffic.failovers`. A reader that does not know them can ignore them; `ui_api` stays `1`.

```json
"fallback": {
  "enabled": true,
  "chain": ["codex", "claude-code", "nous", "openrouter"],
  "primary": "codex",
  "active": "claude-code",
  "cooldown_s": 900.0,
  "cooling": [{"backend": "codex", "reason": "out_of_quota", "until": "2026-10-08T10:15:00Z", "remaining_s": 812}],
  "failovers": 3,
  "last_failover": {"from": "codex", "to": "claude-code", "reason": "out_of_quota", "cooldown_until": "2026-10-08T10:15:00Z", "at": "2026-10-08T10:00:00Z"},
  "source": "env",
  "problems": [],
  "warnings": ["The chain moves from a subscription backend (codex) to a key-billed one (nous, openrouter). ..."]
}
```

`reason` is `out_of_quota` (cooldown `cooldown_s`) or `rate_limited` (the upstream's
`Retry-After`, 60 s without one). Only `out_of_quota` sets a backend row's `state`.
`warnings` says when the chain moves from a subscription backend to a key-billed one.
`active` is the backend the next request tries first. `chain` is `[]` and `enabled` is `false`
when no chain is set. A cooling backend's `state` on its `backends` row is `out_of_quota`.
`problems` lists config Nexus ignored, never a value. Counters and cooldowns are
process-local and reset on restart.

## Added with `ui_api: 1`

New endpoints: `GET /dashboard/config`, `POST /dashboard/bench`. New behavior at `/`: a
built UI is served from `ui/dist` or `$OS3_UI_DIST`.

New fields or behavior on existing endpoints: `backends.<id>.name`, `.kind` (REST rows),
`.state` on `GET /health` and `GET /doctor`; `backend`, `error_code`, `hint` on each
`/dashboard/pulse` entry; `backend` on each `/dashboard/models` row; `activate` (request) and
`activated` (response) on `POST /dashboard/config`; `code` and `hint` on every error body
Nexus produces outside `/v1`; `ui_api` on `GET /health` and `GET /doctor`.

## `GET /dashboard/usage`

`{threshold, warn_percent, critical_percent, auto_switch: {enabled, chain, serving, benched}, backends: [...]}`. Each backend row has `id`, `label`, `active`, `source` (`real` or `estimate`), `windows` (real readings: `used_percent`, `window_minutes`, `resets_in_s`, `stale`, `level`), `estimate` (`requests_1h`, `tokens_1h`, `requests_5h`, `tokens_5h`, counted by Nexus), optional `plan`, `account` (OpenRouter key usage or a note) and `cooling`. A `cooling.reason` of `usage_threshold` is the early switch. The panel hides itself when the route is missing.
