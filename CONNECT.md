# Connect a client to Nexus

This page is client wiring. The install and the first-time Rabbit OS3 walkthrough are in the [README](README.md). Backends, provider terms and settings are in [docs/backends.md](docs/backends.md).

**Which backends work with Rabbit OS3.** OS3 Local and Cipher send tool definitions on their main chat, worker and computer-use requests. Every plan CLI backend (`codex`, `claude-code`, `copilot`, `grok-build`, `antigravity`) refuses tools with HTTP 400 `tools_not_supported`, so OS3 cannot use them. For OS3 use an API-key backend (`openrouter`, `openai`, `anthropic`, `xai`, `nous`) or `chatgpt` (a ChatGPT plan login, at your own risk). OS3 and Cursor are clients, not backends. The risk of using a subscription is in [docs/backends.md](docs/backends.md#provider-risk).

Nexus has to be running. On a clean Mac or Linux host:

```bash
git clone https://github.com/olympusbuildz/nexus.git
cd nexus
./install.sh
./nexus test
```

Installed under the old os3-bridge name? See [docs/upgrading-from-os3-bridge.md](docs/upgrading-from-os3-bridge.md).

`$REPO` below is your checkout: the directory that contains `server.py`.

| Setting | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:11435/v1` |
| API key | the single line in the Nexus key file. `./nexus test` prints the path. A new install uses `$REPO/.nexus_api_key` |
| Auth header | `Authorization: Bearer <that line>` |
| Model | a model the active backend serves, e.g. `openai/gpt-4o-mini` on OpenRouter |

`./nexus test` prints these values for your install and checks them with a real chat and a tool-call round trip. If a client gets HTTP 403 for `127.0.0.1`, try `http://127.1:11435/v1`; the test reports which of the two answers on your machine. We have not seen that 403 ourselves.

The curl example sends no `model`, so Nexus uses the backend's default (a free model on OpenRouter). The other examples below use `${OPENROUTER_MODEL:-openai/gpt-4o-mini}`. That is an example id, not the default, and a paid model on OpenRouter; each backend's default is in [docs/backends.md](docs/backends.md#default-models). Set your own with `export OPENROUTER_MODEL=<id>`. `GET /v1/models` lists what the active backend serves.

```bash
cat "$REPO/.nexus_api_key"
```

`./nexus test` prints the path that is in use. Read the key file only to paste it into the client. Setup writes it mode `600`. The dashboard calls this key the bridge key.

To switch backend, run `./nexus setup` and restart ([docs/service.md](docs/service.md#when-to-restart)). The base URL and the Nexus key stay the same. Use a model id the new backend serves. The backend list is in [docs/backends.md](docs/backends.md).

`./doctor.sh` checks that the process is answering. It leaves the listener running.

If a client appends `/v1/chat/completions` itself, its base URL field is `http://127.0.0.1:11435`. Cursor's override field and the OpenAI SDK want the `/v1` form in the table.

## Address

Every client field uses `127.0.0.1`. Do not substitute localhost. The process binds that address only. A client pointed at localhost can miss it.

## Rabbit OS3 (OS3 Local and Cipher)

This is the main use. OS3 Local runs on the same computer as Nexus: Rabbit's `rabbit-agent` makes the calls to the loopback address below, so nothing is exposed off the machine. Point OS3 at:

- Base URL `http://127.0.0.1:11435/v1`
- API key: the line in `$REPO/.nexus_api_key`
- Model: an id the active backend serves, such as `openai/gpt-4o-mini` on OpenRouter. `GET /v1/models` lists what is live after you switch

`./nexus test` marks its tool-call step SKIP for plan CLI backends, so a PASS there does not mean OS3 will work. The OS3 fields stay the same when you change backend. ChatGPT.com and claude.ai browser sessions are not backends.

Unless you opt in to `./nexus rabbit setup` ([docs/rabbit-setup.md](docs/rabbit-setup.md)), Nexus does not change OS3's saved settings. When port 11435 is already taken, `./nexus start` leaves that process running. `./nexus stop` unloads only the packaged LaunchAgent (`ai.nexus.server`) or `nexus.service` user unit. Rabbit's own UI and in-app wiring live outside this repository.

## Cipher: tools and screenshots

Send these on `POST /v1/chat/completions` with the same bearer. One assistant turn may include several `tool_calls`. The next turn sends one `role: "tool"` message per call, with that call's `tool_call_id`. Screenshots are `image_url` parts. Use a `data:image/png;base64,...` URL (jpeg, gif, and webp work the same way).

```json
{
  "model": "openai/gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "what is on screen?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
      ]
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "click",
        "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}
      }
    }
  ]
}
```

`POST /v1/messages` takes the Anthropic shape: several `tool_use` blocks in one assistant message, `tool_result` blocks for each, and `image` source `base64` or `url`. On the Anthropic backend that JSON is forwarded. On the other backends it is translated into the chat shape above.

This table is the contract. Status payloads do not advertise it.

| Backend | Parallel tool calls | Tool-result round trip | Screenshots |
| --- | --- | --- | --- |
| `openrouter` (default) | forwarded | forwarded | `image_url`, including data URLs and remote URLs the upstream accepts |
| `openai` | forwarded | forwarded | same |
| `xai` | forwarded | forwarded | same |
| `anthropic` | translated to `tool_use` | translated to `tool_result` | data URLs become base64 image blocks; remote URLs stay `source.type: url` |
| `codex` | **400** `tools_not_supported` | **400** `tools_not_supported` | data URLs are temp files on repeated `codex exec --image`. Remote URLs are **400** `image_url_unsupported` (not fetched). Images attach to that exec turn, not to an earlier history turn |
| `claude-code` | **400** `tools_not_supported` | **400** `tools_not_supported` | data URLs are stream-json image blocks (`--input-format stream-json`). Built-in CLI tools stay off. Remote URLs are **400** `image_url_unsupported` |
| `copilot` (alias `github-copilot`) | **400** `tools_not_supported` | **400** `tools_not_supported` | data URLs are temp files on repeated `--attachment`. Remote URLs are **400** `image_url_unsupported` |
| `grok-build` (alias `grok-cli`) | **400** `tools_not_supported` | **400** `tools_not_supported` | **400** `images_not_supported`. Headless input is text. `grok` remains the xAI API |
| `antigravity` (alias `agy`) | **400** `tools_not_supported` | **400** `tools_not_supported` | **400** `images_not_supported`. Headless stdin is text. Not the retired consumer Gemini CLI |

Free and small models often get OS3's strict tool calls slightly wrong. On `POST /v1/chat/completions` and `POST /v1/messages` with `tools`, streaming or not (on `/v1/messages` only for backends reached through chat; the native `anthropic` backend is forwarded untouched), Nexus checks each returned call (every call in a turn) against the tool schemas in the request: the tool exists, the arguments are a JSON object, required fields are present, and types and enums match. It fixes only what is mechanical, before OS3 sees it:

- arguments: code fences, trailing junk after the object, one trailing comma, double encoding, stray backslashes, literal newlines
- tool name: a different case or stray spaces around it, when exactly one tool matches (never a prefix)
- values: a numeric string where the schema wants a number or integer; keys the schema does not allow, only when it sets `additionalProperties: false` and does not use `allOf`/`anyOf`/`oneOf`/`$ref`
- OS3 specifics: a device name or garbled id in `node_id`, a bare action as the `computer_use` script, a capture with no `feed_image`, a missing call id

A missing required field is never invented. If a call is still invalid the model is asked once more (chat backends only). If it is still invalid, OS3 gets a clean error instead of a call that would fail: HTTP 502 with `code: "invalid_tool_call"` and a `hint`, or on a stream that has already started, an error event. An answer cut off by the token limit (`finish_reason: length`) and an answer that needed nothing are passed through byte for byte. `GET /dashboard/pulse` counts `tool_repair.repaired` and `tool_repair.rejected` since the process started. Set `NEXUS_TOOL_REPAIR=off` to turn all of this off. A call written as prose instead of `tool_calls` is not rescued.

### Per-role models

OS3's local mode has one model field, but it sends different kinds of request. On `POST /v1/chat/completions` Nexus tells them apart and can send each to its own model on the active backend:

| Role | OS3 calls it | How it is recognised |
| --- | --- | --- |
| `chat` | Small, the main assistant | tools like `create_task` or `steer_task`; any tool Nexus does not know |
| `worker` | Standard, runs tools | a "You are a worker agent" system prompt, or tools like `shell`, `file_read`, `computer_use` |
| `background` | memory, facts, review | no tools, or a data-only tool such as `emit_facts` |
| `vision` | the image-understanding check | an image and no tools |

Set a role with `NEXUS_MODEL_CHAT`, `NEXUS_MODEL_WORKER`, `NEXUS_MODEL_BACKGROUND`, or `NEXUS_MODEL_VISION`, or put the same names in a JSON file, `.nexus_roles.json` (override the path with `NEXUS_ROLES_FILE`):

```json
{"chat": "vendor/chat-model:free", "worker": "vendor/tool-model:free", "background": "vendor/small-model:free"}
```

The environment wins over the file, per role. Use the file for a packaged service, which does not inherit your shell. Both are read on every request; no restart is needed. A role with no setting keeps the model OS3 sent (the Nexus default when OS3 sent none). A role with a setting wins over OS3's model field, and the log line says only `role=worker model override applied`.

The image check goes to the `vision` model when you set one, otherwise to the `worker` model (a model you trust with screenshots), otherwise to OS3's model. Screenshots a worker sends during a task carry tools, so they stay `worker` traffic.

`/health` and `/doctor` list `roles` with the effective model for each, and `ready` is true only when every model you configured is live on the active backend (`roles_ready`, `roles_hint`). The dashboard shows the same line under Model. Roles apply to `/v1/chat/completions`, which is what OS3 sends; `/v1/messages` clients are not routed.

Stream and non-stream both carry tools and images on the API backends. Codex, Claude Code, and Copilot carry inline screenshots on both. Up to 16 inline images, 12 MiB each, png / jpeg / gif / webp, where that CLI accepts them.

### Unknown model ids

A client may send a model id the active backend does not serve (an OpenRouter id while Nous is active). On `POST /v1/chat/completions`, when that id is not in the backend's live model list (the list `GET /v1/models` serves), Nexus sends the request to the active model (`NEXUS_MODEL`, the saved default, or the backend default) instead of letting the upstream answer 404. The response `model` is the model that was served, and the response carries `X-OS3-Model-Remapped: <requested id>`, on streams too. One INFO line per remap names the two ids, nothing else, and `GET /dashboard/pulse` counts them as `traffic.model_remapped`.

Matching is an exact, trimmed, case-insensitive comparison against the list (the canonical id is served, with no header). Nothing is remapped when the list could not be read or is empty, when a role model is set for the request, when no model was sent, on the subscription CLI backends (they resolve the model themselves), or on `/v1/messages`. The list is cached for 60 seconds. Set `NEXUS_MODEL_FALLBACK=off` to forward every id unchanged.

## ChatGPT plan with tools

OS3's main chat, worker and computer-use requests carry `tools`, and subscription CLI backends refuse tools. To run OS3 on a ChatGPT plan, use the `chatgpt` backend (at your own risk; see [ChatGPT plan backend](docs/backends.md#chatgpt-plan-backend)), not `codex`:

```bash
codex login                       # choose Sign in with ChatGPT, once
./nexus setup --backend chatgpt   # or Settings, or POST /dashboard/config {"backend":"chatgpt"}
curl -s http://127.0.0.1:11435/health | jq '.backend, .ready, .backends.chatgpt.state'
```

Keep a fallback for when the plan's limit is hit: `{"chain": ["chatgpt", "nous"]}` in `.nexus_fallback.json`.

## Images (OS3 Images setting)

`POST /v1/images/generations` takes the OpenAI Images body (`prompt`, `model`, `size`, `quality`, `n` 1-4, `background`, `output_format`, `response_format`) and returns `{created, data: [{b64_json, revised_prompt?}]}`. `response_format: "url"` returns a `data:` URL, not a hosted link. `/v1/images/edits` and `/v1/images/variations` answer 501.

- **ChatGPT plan (default when `codex login` is present):** the plan's own Images endpoint with `gpt-image-2`; if the plan refuses it (404 or "unsupported"), Nexus falls back to Responses with the hosted `image_generation` tool. Images draw on the same plan windows as chat; a usage limit is HTTP 429 `chatgpt_quota`. Like the chatgpt chat backend this uses an unofficial path: at your own risk.
- **Nous (off by default, billed per image):** opt in with `NEXUS_ENABLE_NOUS_IMAGES=1`; capped by `NEXUS_NOUS_IMAGE_DAILY_CAP` (default 10 per rolling 24 h). `NEXUS_IMAGES_BACKEND=chatgpt|nous|off` picks one explicitly.
- `GET /v1/models` lists `gpt-image-2` when the plan login is ready, so OS3's verify step accepts it. `/dashboard/usage` and the usage panel count images per 1 h / 5 h / 24 h.
- Diagnostics: the request header `X-Nexus-Image-Route: responses` (or `NEXUS_IMAGES_FORCE_RESPONSES=1`) skips straight to the Responses fallback. The response header `X-Nexus-Image-Route` says which path answered.

In OS3: Settings > API keys > Images > add image model > generate images. For the key, reuse the saved **local connection** (the same Nexus connection chat uses, which runs on your computer through rabbit-agent), and set the model to `gpt-image-2`. Then verify; OS3 checks `/v1/models` and generates one test image, so verify spends one plan image. Use the local connection, not "Custom provider": a custom provider is called from OS3's servers, which cannot reach `127.0.0.1` on your machine. If you add a new local key instead, the base URL is `http://127.0.0.1:11435/v1` and the key is the Nexus key in `.nexus_api_key`.

When a task needs to look at an image, OS3 may also ask you to connect "understand images" (a vision model). Connect it to the same saved local connection with any model id from its list: Nexus sends the image to the ChatGPT plan as `input_image` (unknown ids are remapped to the plan model). A Nous fallback is used for images only if its model takes image input.

## Fallback at a usage limit

A subscription runs out. When the active backend hits its usage limit, Nexus can retry the same request on the next backend in a chain you name, so OS3 keeps working instead of showing a 429. It is **off until you configure a chain**: with one backend, behavior is exactly what it was.

```bash
export NEXUS_FALLBACK="codex,claude-code,nous,openrouter"   # ordered; the active backend always goes first
export NEXUS_FALLBACK_COOLDOWN=900                          # seconds an exhausted backend sits out (default 900)
```

A packaged service does not inherit your shell. Put the same thing in `.nexus_fallback.json` (override the path with `NEXUS_FALLBACK_FILE`); the environment wins over the file:

```json
{"chain": ["codex", "claude-code", "nous", "openrouter"], "cooldown_s": 900}
```

Both are read on every request; no restart is needed. `NEXUS_FALLBACK=off` turns it off.

- **Only a limit fails over.** HTTP 429 or a provider quota/credit body from an API backend, or the "usage limit reached" output of a subscription CLI. A rejected key, a logged-out CLI, a bad request, a timeout, or a 5xx is returned to OS3 as before. CLI wording changes; a message Nexus does not recognise is not retried.
- **A rate limit is not a spent plan.** A 429 with no quota or credit body (and a CLI "rate limit" or `RESOURCE_EXHAUSTED` with no quota wording) is a per-minute style limit: the request moves on, but that backend sits out only the upstream's `Retry-After`, or 60 s without one (never more than the cooldown). Only a real quota or usage-limit signal gets the long cooldown.
- **Before the first byte only.** The retry happens before anything is streamed to the client. A stream that dies half-way is not retried.
- **Present, capable backends only.** A backend whose CLI is missing or signed out, or whose key is absent, is skipped. So is one that cannot serve the request: a request with `tools` never goes to a subscription CLI, and one with an image never goes to `grok-build` or `antigravity`. If a fallback still refuses (`tools_not_supported`, `images_not_supported`, `image_url_unsupported`, or the model is not found there), it is skipped with no cooldown and the walk continues. The active backend's own refusals are returned as before.
- **Cooldown.** An exhausted backend is skipped for the cooldown, then tried first again. State is in memory and resets on restart. A failover is counted and logged only when a backend answers.
- **Models.** Each backend uses its own model: the one saved for it, or its default. The model OS3 asked for, per-role models, and `NEXUS_MODEL` apply to the active backend only.
- **All exhausted.** If every backend that was tried hits a limit, OS3 gets **429** `fallback_exhausted` with a hint naming what was tried. If nothing else could serve, OS3 gets the active backend's own error.
- Applies to `POST /v1/chat/completions` and to `POST /v1/messages` on every backend except a native `anthropic` primary, which streams Messages straight through.
- A request moved to another provider sends that provider the same conversation. Name only backends you are willing to send OS3's traffic to.

`GET /health` shows it under `fallback`: the effective `chain`, the `active` backend, which are `cooling` (with `reason`, `out_of_quota` or `rate_limited`) and `until` when, the `failovers` count, and `warnings`. A response served by a backend other than the active one carries an `X-OS3-Backend` header naming it; a normal response does not. `./doctor.sh` prints the chain and warns when it moves from a subscription backend to a key-billed one (`openrouter`, `openai`, `anthropic`, `xai`, `nous`). `GET /dashboard/pulse` carries the same `fallback` object and `traffic.failovers`; each call's `backend` is the one that answered. The log gets one line per failover, for example `failover codex -> claude-code reason=out_of_quota cooldown_until=2026-10-08T10:15:00Z`.

## Cursor

Cursor is a client. It is not a backend. Prefer the Rabbit OS3 section above when that is your destination.

Settings → Models:

1. OpenAI API Key: paste the Nexus key from `$REPO/.nexus_api_key`.
2. Enable **Override OpenAI Base URL** and set `http://127.0.0.1:11435/v1`.
3. Add a model the active backend serves, for example `openai/gpt-4o-mini` on OpenRouter. Select it.

Menu names move. The base URL, the bearer, and the model id are the contract.

## OpenAI-compatible base URL

Use this for the OpenAI Python SDK and for any client that has a custom OpenAI base URL field. The ChatGPT consumer app does not have that field. ChatGPT.com is not a backend. See [docs/backends.md](docs/backends.md).

```python
import os
from pathlib import Path

from openai import OpenAI

key = Path(".nexus_api_key").read_text().strip()  # run from your checkout
client = OpenAI(base_url="http://127.0.0.1:11435/v1", api_key=key)
reply = client.chat.completions.create(
    model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),  # your own model id
    messages=[{"role": "user", "content": "Reply with exactly: ok"}],
)
```

## Claude Code as a client

This section points the Claude Code **client** at Nexus. It is the opposite of `NEXUS_BACKEND=claude-code`. That backend starts the `claude` CLI and uses your Claude subscription as the upstream. The alias `claude` is the Anthropic **API** (`ANTHROPIC_API_KEY`). The subscription CLI is `claude-code` (alias `claude-cli`). Subscription backends work only when that official CLI is installed and logged in. [docs/backends.md](docs/backends.md) lists which CLIs are real backends. The deprecated `gemini` binary is not one of them. Use `agy` (`antigravity`).

Claude Code calls `POST /v1/messages`. On OpenRouter, OpenAI, xAI, and the subscription CLIs Nexus translates the body to a chat completion. On `NEXUS_BACKEND=anthropic` it forwards native Messages. The client still sends the Nexus key as the bearer. The provider key stays on the machine running Nexus.

`ANTHROPIC_BASE_URL` is the origin. Claude Code appends `/v1/messages`. `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer`. `ANTHROPIC_API_KEY` (`x-api-key`) is not the header this process checks.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:11435
export ANTHROPIC_AUTH_TOKEN="$(tr -d '\n' < "$REPO/.nexus_api_key")"
export ANTHROPIC_MODEL="${OPENROUTER_MODEL:-openai/gpt-4o-mini}"   # set your own
```

On OpenRouter, a model id with no `/` that starts with `claude` is sent upstream as `anthropic/<id>`. Any other id, including a namespaced one like `openai/gpt-4o-mini`, is forwarded as written. Streaming and non-streaming both work. Text, images, and tool use are translated. Prompt-cache markers and extended thinking blocks are dropped on that translated path.

On native Anthropic they are forwarded with the JSON body. A model id prefixed `anthropic/` is stripped before the upstream call. Use an Anthropic model id (`claude-sonnet-4-5` unless you set `NEXUS_MODEL`). OpenAI and xAI do not rewrite model ids.

On the subscription CLIs, the translated route accepts text. Codex, Claude Code, and Copilot also accept inline screenshots. Grok Build and Antigravity return 400 for images. Client tool calls return 400. `max_tokens` is not enforced on the CLI. Using a personal subscription through the official CLI can violate that provider's terms. See [Provider risk](docs/backends.md#provider-risk).

## curl

```bash
KEY=$(tr -d '\n' < "$REPO/.nexus_api_key")
curl -sS http://127.0.0.1:11435/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Reply with exactly: ok"}],"stream":false}'
```

Models:

```bash
curl -sS http://127.0.0.1:11435/v1/models \
  -H "Authorization: Bearer $KEY"
```

Health has no auth: `curl -sS http://127.0.0.1:11435/health`

| Method | Path | Auth |
| --- | --- | --- |
| `GET` | `/health` | none |
| `GET` | `/doctor` | none |
| `GET` | `/v1/models` | bearer |
| `POST` | `/v1/chat/completions` | bearer |
| `POST` | `/v1/messages` | bearer |

`/health` and `/doctor` do not claim tool or image support.

## rabbit-agent watchdog (macOS, optional)

OS3 reaches Nexus through Rabbit's `rabbit-agent`. If that process crashes, OS3 stops working while `/health` still says ready. Set `NEXUS_RABBIT_WATCHDOG=on` before `./nexus start` and the running service checks Rabbit's LaunchAgent (`com.rabbit.rabbit-agent`; override with `NEXUS_RABBIT_AGENT_LABEL`) every 30 seconds with `launchctl print`. It restarts only when launchd's `state` is exactly `not running` and the last end was a crash (a non-zero exit code, named or not, or a signal other than SIGTERM/SIGINT/SIGHUP), on two checks in a row. It then runs `launchctl kickstart gui/<uid>/com.rabbit.rabbit-agent`. Every launchctl call has a 10 second limit; a timeout means no action.

Anything it cannot read as a crash means no action: `spawn scheduled`, a running job, a clean exit, a jetsam exit with no exit code, a missing job, or output it does not recognise. It cannot tell an idle tunnel from a stuck one, so it never acts on a quiet agent or missing OS3 traffic. It never uses `-k`, kills by pid, port, or name, and refuses a label that is one of Nexus's own.

At most 3 restarts per hour, counted across restarts of Nexus: the times are kept in `.nexus_rabbit_watchdog.json` (mode 600, next to the other state files; `NEXUS_RABBIT_WATCHDOG_STATE` moves it for `./nexus run`; the packaged service does not receive that variable). If that file cannot be read or written, it does not restart. Delete a damaged file to reset. Not-loaded, unrecognised output, and a refused label are logged once each.

With the variable unset, `/health` and `/doctor` are unchanged. With it set they carry `watchdog`: `enabled`, `last_check`, `last_restart`, `restart_count`, `last_reason`. Linux and Windows: nothing runs.

## What this repo cannot fix

- Rabbit is closed source. Its UI and in-app wiring are not in this tree.
- Screen Recording TCC needs a click on the macOS consent dialog.
- OpenRouter credits are on the OpenRouter account. An empty balance fails chat upstream while the Nexus process is healthy.
