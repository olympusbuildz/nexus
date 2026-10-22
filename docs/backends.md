# Backends

A backend is where Nexus sends each request: an API key, the `chatgpt` plan login, or an official CLI you are signed in to. Nexus runs one backend at a time. To switch, run `./nexus setup`, then restart as described in [service.md](service.md#when-to-restart).

## Which backends work with Rabbit OS3

OS3 sends tool definitions (`tools`) on its main chat, worker and computer-use requests. Every plan CLI backend refuses a request that carries tools with HTTP 400 `tools_not_supported`, so OS3 cannot use them. API-key backends and `chatgpt` accept tools. The summary table is in the [README](../README.md#provider-risk-and-backend-choice).

The claim that OS3 sends tools rests on the request shapes in `tests/fixtures/os3/`. They are reconstructed from public OS3 tooling and are not a capture from a real OS3 build. Background requests (memory, facts) can arrive without tools; see [CONNECT.md](../CONNECT.md#per-role-models).

Model names and tool-call quality are up to the model, not Nexus. The OpenRouter default `google/gemma-4-31b-it:free` is only a starting guess: no test in this repository runs it against OS3, so whether it handles OS3's tool calls is unverified. Free and small models often get strict tool calls slightly wrong. Nexus repairs the mechanical mistakes (see [CONNECT.md](../CONNECT.md)), but if OS3 tasks fail, pick a stronger model.

## How you authenticate

API-key backends need a key file written by `./nexus setup`, or the environment variable in a foreground shell (`./nexus run`). The packaged service reads the key file only, because it does not inherit your shell.

| Backend | Authentication |
| --- | --- |
| `openrouter` (default) | API key. Env var `OPENROUTER_API_KEY` |
| `openai` | API key. `OPENAI_API_KEY` |
| `anthropic` (alias `claude`) | API key. `ANTHROPIC_API_KEY`. This alias is the API, not Claude Code |
| `xai` (alias `grok`) | API key. `XAI_API_KEY` |
| `chatgpt` (alias `chatgpt-plan`) | The ChatGPT sign-in that `codex login` saved (Sign in with ChatGPT). No key, no CLI process per request. See [ChatGPT plan backend](#chatgpt-plan-backend) |
| `codex` | `codex` CLI (`npm i -g @openai/codex` or the Homebrew cask), then `codex login`. Probe: `codex login status` |
| `claude-code` (alias `claude-cli`) | `claude` CLI (`brew install --cask claude-code` or `npm i -g @anthropic-ai/claude-code`), then `claude auth login`. Probe: `claude auth status`. The health check never runs `auth login` |
| `copilot` (alias `github-copilot`) | `copilot` binary (`npm i -g @github/copilot` or `brew install copilot-cli`), then `copilot login` or `/login`. Not `gh`. Probe: `copilot version` |
| `grok-build` (alias `grok-cli`) | `grok` CLI, then `grok login` or `grok login --device-auth`. Probe: `grok models`. With no login it prints `You are not authenticated.` and still exits 0, so Nexus treats that line as `missing` and its absence as `ok`. `ready` follows the login |
| `antigravity` (alias `agy`) | Off unless you set `NEXUS_ENABLE_ANTIGRAVITY=1`. `agy` from `brew install --cask antigravity-cli`, or `curl https://antigravity.google/cli/install.sh`. Sign in by running `agy` (Google account or API key). Probe: `agy models`. A model list is not a login |
| `nous` | Advanced. See [Nous Portal](#nous-portal-advanced) |

Subscription backends run an official CLI that you installed and signed in to. Setup lists a CLI only when its binary is on `PATH`; a missing one is named with its install command and is not selected. `/health` reports `ready` only when the key or login is usable. A missing or logged-out CLI makes chat return 503.

Names that look alike: `claude` is the Anthropic API and `claude-code` is the subscription CLI. `grok` is the xAI API and `grok-build` is the Grok Build CLI. `gh` is not `copilot`. Prefer `antigravity` over the deprecated `gemini` binary.

These are not backends, and `/health` neither lists nor marks them ready:

| Account | Why |
| --- | --- |
| ChatGPT.com in the browser | Not a backend. The ChatGPT plan backends are `chatgpt` (HTTP, tools) and `codex` (CLI, no tools) |
| claude.ai in the browser | No CLI here. The subscription backend is `claude-code` |
| Perplexity | No official CLI Nexus runs |
| Cursor | A client, through Override OpenAI Base URL |
| Grok on the web | Not a backend. See `xai` and `grok-build` |
| Consumer Gemini CLI (`gemini`) | Deprecated. The successor is `agy` / `antigravity` |

## Provider risk

Using a personal subscription through anything other than the vendor's own app may break that provider's terms. The provider could limit or suspend the account, and the terms change. The risk ratings in the README are our judgment, not legal advice: read the linked terms and decide for yourself.

| Backend | What it uses | Default state |
| --- | --- | --- |
| `codex` | The official `codex` CLI, signed in with your ChatGPT plan. [OpenAI terms](https://openai.com/policies/terms-of-use/) | On when the CLI is installed and signed in |
| `chatgpt` | Your ChatGPT plan's Codex login, used by Nexus itself (not by the CLI) against the endpoint the Codex clients use, with its own token refresh. It is not an official client or a public API. [OpenAI terms](https://openai.com/policies/terms-of-use/) | Selectable when a ChatGPT `codex login` is present. Never active unless you choose it |
| `claude-code` | The `claude` binary, run as the vendor ships it. Anthropic's terms govern Claude Code; we have not confirmed that serving other apps through it is permitted. [Anthropic consumer terms](https://www.anthropic.com/legal/consumer-terms) | On when the CLI is installed and signed in |
| `copilot` | The `copilot` CLI, signed in with your GitHub Copilot plan, driven non-interactively. [GitHub Copilot terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features) | On when the CLI is installed and signed in |
| `antigravity` | The `agy` CLI, signed in with a Google account or API key. [Antigravity terms](https://antigravity.google/terms) | Off. Set `NEXUS_ENABLE_ANTIGRAVITY=1` to opt in |
| `grok-build` | The `grok` CLI, signed in with your xAI plan. [xAI terms](https://x.ai/legal/terms-of-service) | On when the CLI is installed and signed in |
| `nous`, `openrouter`, `openai`, `anthropic`, `xai` | A provider API billed to your key. [Nous](https://portal.nousresearch.com/terms), [OpenRouter](https://openrouter.ai/terms), [OpenAI](https://openai.com/policies/business-terms/), [Anthropic](https://www.anthropic.com/legal/commercial-terms), [xAI](https://x.ai/legal/terms-of-service) | On once a key is configured |

"On" means selectable, not active. Setup lists every backend, but a subscription CLI can be chosen only when its binary is on `PATH` (the menu marks it "not installed"). While `antigravity` is off, setup refuses it, chat returns 403 `backend_disabled`, and `/health` and `/doctor` show `ready: false` with a hint naming the switch. For the packaged service, export `NEXUS_ENABLE_ANTIGRAVITY=1` in the shell before `./nexus start`.

Your prompts go to whichever provider you pick, whatever the backend. Only the endpoint is local.

## Default models

Used when `NEXUS_MODEL` is unset and no model is saved. They are starting guesses, not entitlements: whether your account can use one is up to the provider.

| Backend | Default model |
| --- | --- |
| `openrouter` | `google/gemma-4-31b-it:free` (`DEFAULT_MODEL` in `src/nexus/config.py`) |
| `nous` | `meituan/longcat-2.5-preview:free` |
| `openai` | `gpt-4o-mini` |
| `anthropic` | `claude-sonnet-4-5` |
| `xai` | `grok-4` |
| `codex` | `gpt-5.6-sol` |
| `chatgpt` | `gpt-6-luna` |
| `claude-code` | `claude-sonnet-4-5` |
| `copilot` | `claude-sonnet-4.6` |
| `grok-build` | `grok-build` |
| `antigravity` | `gemini-3.8-flash-high` |

Set `NEXUS_MODEL` in the shell before `./nexus start` to bake a different id into the service. A model saved from the dashboard or `./nexus setup` is written to `.nexus_model`. `NEXUS_MODEL` in the process environment wins over that file until the process restarts. When `./nexus run` starts (the packaged service runs the same command) it checks the default model against the active backend and the API-key backends, and refuses to start when none serves it and at least one confirmed it is missing.

## Where keys and choices are stored

Only the Nexus key and the choice files are in the checkout. Provider keys are outside it.

| What | Where |
| --- | --- |
| Nexus key (what clients send as the bearer) | `<checkout>/.nexus_api_key`. Override with `NEXUS_KEY_FILE` |
| Active backend, default model | `<checkout>/.nexus_backend`, `<checkout>/.nexus_model` |
| Model per OS3 role, failover chain | `<checkout>/.nexus_roles.json`, `<checkout>/.nexus_fallback.json` |
| Usage tracker, Rabbit state | `<checkout>/.nexus_usage.json`, `.nexus_rabbit_watchdog.json`, `.nexus_rabbit_backup.json` |
| Provider keys, macOS | `~/Library/Application Support/Nexus/`: `.openrouter-os3.key`, `.openai-os3.key`, `.anthropic-os3.key`, `.xai-os3.key`, `.nous-os3.key` |
| Provider keys, Linux | `$XDG_CONFIG_HOME/nexus/` (default `~/.config/nexus/`): `openrouter.key`, `openai.key`, `anthropic.key`, `xai.key`, `nous.key` |

Override a provider key path with `NEXUS_OPENROUTER_KEY_FILE`, `NEXUS_OPENAI_KEY_FILE`, `NEXUS_ANTHROPIC_KEY_FILE`, `NEXUS_XAI_KEY_FILE` or `NEXUS_NOUS_KEY_FILE`. Those overrides are read by `./nexus run` and the CLI; the packaged service does not receive them (see [service.md](service.md#what-the-service-receives)).

Every key file is written with mode `600`; do not loosen it. Setup refuses to continue if it cannot set that mode. Keys never appear in `/health`, `/doctor`, the dashboard page or logs. Installs made under the old os3-bridge names are covered in [upgrading-from-os3-bridge.md](upgrading-from-os3-bridge.md).

## What Nexus runs for each CLI backend

After you are signed in, one request starts one child process:

- `codex`: `codex exec --json --skip-git-repo-check --ignore-user-config -s read-only -c approval_policy="never" -C <temp dir> -m <model>`, prompt on stdin, `--image <file>` per screenshot.
- `claude-code`: `claude -p --model <model> --output-format json|stream-json --no-session-persistence --tools "" --strict-mcp-config --disable-slash-commands --setting-sources user`, so its own tools are off and project settings are not loaded.
- `copilot`: the transcript is piped to `copilot -s --no-ask-user --no-color --no-custom-instructions --output-format json --model <model> --excluded-tools <list>`. The pipe form keeps the prompt off the command line.
- `grok-build`: `grok -p <transcript> --output-format json|streaming-json -m <model> --cwd <temp dir> --sandbox read-only --max-turns 1 --no-auto-update`.
- `antigravity`: `agy --input-format stream-json --output-format stream-json --model <model> --sandbox`, transcript on stdin.

The child process does not receive provider API keys, including `XAI_API_KEY` and `GEMINI_API_KEY`, so the CLI uses its subscription login. Each request gets its own temporary directory, which is removed afterwards.

`./nexus start` records an absolute path for each subscription CLI it can see. The packaged service `PATH` starts with `/opt/homebrew/bin:/usr/local/bin` and the absolute `$HOME/.grok/bin`, then the system directories. If a binary is not on `PATH`, set `NEXUS_CODEX_BIN`, `NEXUS_CLAUDE_BIN`, `NEXUS_COPILOT_BIN`, `NEXUS_GROK_BIN` or `NEXUS_AGY_BIN` before `./nexus start`.

`/health` sets `cli_present` when the binary exists. `implemented` is true for a subscription CLI only after the dry probe has started the binary. `ready` is true only when that same output reports a usable login (`logged in` or `signed in`). A version banner, a model catalog and a token environment variable are not a login. Probe output is not copied into `/health` or `/doctor`. A missing binary stays `implemented: false`.

Each backend row in `/health` also carries the model ids it offers and says where they came from. `models_source: "cli"` means the list is the account's own: `grok-build` and `antigravity` parse it out of the `models` probe they already run, so a dashboard refresh starts no extra process. `models_source: "catalog"` means Nexus's own starting set, not an entitlement. `codex`, `claude-code` and `copilot` have no model-listing command, so they always report `"catalog"`. The API-key backends also report `"catalog"` in `/health`, but `GET /v1/models` reads their live list from the account. `models_hint` says the same in words. A subscription backend whose list came from the catalog serves each row from `GET /v1/models` with the `unverified:` warning attached, so a client that reads only `id` still sees the caveat.

## ChatGPT plan backend

`chatgpt` sends requests to the endpoint the official Codex clients use (`chatgpt.com/backend-api/codex/responses`), signed in with your ChatGPT plan. It is not the public OpenAI API and it is not documented for third-party use. OpenAI may consider this outside what your plan allows, may change or block it without notice, and usage counts against your plan's Codex limits. Read the [OpenAI terms](https://openai.com/policies/terms-of-use/) and decide for yourself. For the lowest-risk use of OpenAI, use an OpenAI API key (`openai`).

`chatgpt` speaks the Responses API directly, so requests that carry tools work like they do on any API backend.

- Chat completions (with `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning_effort`, `response_format` and `image_url` data URLs) are translated to Responses and back. Tool calls come back as normal `tool_calls`, streamed or not. `/v1/messages` is translated through the same path.
- `GET /v1/models` lists the models the plan offers, read from the Codex models endpoint. Set `NEXUS_CHATGPT_CLIENT_VERSION` if it lists too few. Default model `gpt-6-luna`.
- Credentials: Nexus reads the Codex login file (`$CODEX_HOME/auth.json`, default `~/.codex/auth.json`; override with `NEXUS_CHATGPT_AUTH_FILE`) at request time. When the access token is within 5 minutes of expiry, or the backend answers 401, Nexus refreshes it with the refresh token, the same way the Codex CLI does, at `auth.openai.com`. The refresh runs under a file lock (`auth.json.nexus.lock`, created beside the login file) and writes the new tokens back to the same file atomically with mode 600, so the CLI and Nexus keep sharing one login. Tokens are never logged, copied or returned. An API-key Codex login is not a ChatGPT login and is reported as not ready.
- Readiness: `/health` shows `chatgpt` ready when that file holds a ChatGPT login. If the refresh token is rejected, chat returns `chatgpt_not_logged_in`; run `codex login` again.
- Transient failures: if opening the upstream request fails with a TLS or connection error, Nexus retries once after 0.3 to 0.5 seconds, before any byte reaches the client. An HTTP status is never retried.
- Usage limit: a 429 or `usage_limit_reached` is `chatgpt_quota` (HTTP 429) and counts as a spent plan for the [fallback chain](../CONNECT.md#fallback-at-a-usage-limit), for example `{"chain": ["chatgpt", "nous"]}`.

Switch to it with `./nexus setup --backend chatgpt`, or Settings, or `POST /dashboard/config {"backend": "chatgpt"}`. No restart is needed.

## Nous Portal (advanced)

Not part of the quick start. `nous` reads an OAuth access token, not a plain API key, and that token expires every 60 minutes. The path is coupled to Hermes Agent: run `hermes auth add nous` once. After that Nexus keeps the token live itself. `./nexus start` copies the token from `~/.hermes/auth.json` into a mode-`600` file (`.nous-os3.key` on macOS, `nous.key` on Linux, in the provider key folder above; override with `NEXUS_NOUS_KEY_FILE`) before it installs the unit, and refuses to start with a clear message when the Hermes token is missing or expired. `./nexus run` and service restarts do the same check, and the running service copies it again every 45 minutes, so there is no hourly job to schedule. `python3 scripts/sync-nous-token.py` is a one-shot copy. A static `NOUS_API_KEY` also works and skips the sync. Without Hermes Agent, ignore this backend.

## Usage numbers: from the provider or estimated

The dashboard's Usage panel labels every number.

- From the provider: plan and rate-limit windows (ChatGPT plan `x-codex-primary-*` and `x-codex-secondary-*` headers, and the read-only `chatgpt.com/backend-api/wham/usage` endpoint, read at most once a minute while the dashboard is open and off with `NEXUS_CHATGPT_USAGE_POLL=0`; `x-ratelimit-*` headers from Nous, OpenAI, xAI and OpenRouter, refreshed on every call through that backend; OpenRouter `/key` credit), each with the age of the reading. A window whose reset time has passed shows as unknown until the next call through that backend.
- From the provider: token counts, the `usage` numbers in each response, streamed or not. Nexus asks OpenAI-style backends for `include_usage` on streams and drops that extra chunk again unless the client asked for it. `NEXUS_STREAM_USAGE=0` turns the request off for a provider that rejects it. A call that reported no usage counts zero tokens; it is never guessed.
- Estimate: request counts per backend over 1 h and 5 h. Nexus counts them itself.

## Fallback chain at a usage limit

Set `NEXUS_FALLBACK="codex,claude-code,nous,openrouter"` or write `.nexus_fallback.json`, and a request that hits the active backend's usage limit is retried on the next present backend, before any bytes reach the client. The packaged service reads the file, not the variable. The exhausted backend cools down for `NEXUS_FALLBACK_COOLDOWN` seconds (default 900), then goes first again. Off unless a chain is set. Details: [CONNECT.md](../CONNECT.md#fallback-at-a-usage-limit).

## Images

`POST /v1/images/generations` serves OpenAI-style image generation from the ChatGPT plan: `gpt-image-2` through the plan's Images endpoint, falling back to the Responses `image_generation` tool when the plan refuses that endpoint. It returns `{created, data: [{b64_json, revised_prompt?}]}`; `response_format: "url"` returns a `data:` URL. `/v1/images/edits` and `/v1/images/variations` answer 501.

```bash
curl -s http://127.0.0.1:11435/v1/images/generations \
  -H "Authorization: Bearer $(cat .nexus_api_key)" \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-image-2","prompt":"a small red apple","size":"1024x1024","quality":"low"}' \
  | python3 -c 'import sys,json,base64; open("out.png","wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'
```

- Image calls draw on the same ChatGPT plan windows as chat. A usage limit is HTTP 429 `chatgpt_quota`. `/dashboard/usage` counts images per 1 h, 5 h and 24 h.
- Nous images are off by default because Nous bills per image. Opt in with `NEXUS_ENABLE_NOUS_IMAGES=1`, capped by `NEXUS_NOUS_IMAGE_DAILY_CAP` (default 10 per rolling 24 h). `NEXUS_IMAGES_BACKEND=chatgpt|nous|off` pins one.
- The ChatGPT plan image path is unofficial, like the `chatgpt` chat backend: use it at your own risk.

Image inputs in chat (OpenAI `image_url` parts, `data:` or `https:`) go to the ChatGPT plan as Responses `input_image`, whatever model name the client sends (an unknown id is remapped to the plan's model). A fallback backend gets an image-bearing request only if its model lists `image` in its `/models` input modalities; otherwise it is skipped and the error names it under `fallback.skipped`.

OS3 setup for images is in [CONNECT.md](../CONNECT.md#images-os3-images-setting).

## Doctor and health

```bash
./doctor.sh
NEXUS_DOCTOR_SKIP_CHAT=1 ./doctor.sh
NEXUS_DOCTOR_SKIP_SMOKE=1 ./doctor.sh
NEXUS_DOCTOR_SKIP_CRED=1 ./doctor.sh
```

`./doctor.sh` prints PASS or FAIL. FAIL is unstructured stderr from curl or the shell, then a structured JSON object on stdout with `ok`, `checks` and `failures`. The live chat check asks for the exact reply `ok`. Smoke is a second chat that expects the same. The skip flags are opt-in.

The provider-credential line proves the configured key, not just its file: a placeholder shape fails outright, and one authenticated call to the upstream fails the run when the provider refuses the key. Subscription backends have no key file, so the check skips them, and skip-cred skips the call. Setup runs the same check before it writes or keeps a key.

`GET /health` returns JSON: process up, active backend, and a row for every known backend. Each row has `implemented`, `ready`, `cli_present` (subscription only), `detail`, `models`, `models_source` and `models_hint`. The active row also has `model_configured`, `model_ready` and `model_hint`. `ready` is true only when a chat request would work, which includes the provider accepting the key: a refused key is `ready: false` with `upstream_status` and a `model_hint` saying so, even where the provider's model list is public (OpenRouter is checked with `GET /key`). `model_ready` is a separate probe of the configured model id. `ok` means the process answered; it does not claim the active backend is ready. The payload has no key material. Prefer `/health` or `/doctor` over reading unit logs.

`/health` does network I/O. Deciding `ready` means asking the upstream whether the configured model is real, so a cold cache waits on that request and a warm one answers in milliseconds. That wait has its own budget, `NEXUS_HEALTH_TIMEOUT` (default 2 seconds), independent of `NEXUS_UPSTREAM_TIMEOUT` (default 120 seconds), which is the chat path's budget. When the cold-cache check exceeds the health timeout, `/health` reports `model_ready: null` with a `model_hint` instead of blocking the poll. Raise `NEXUS_HEALTH_TIMEOUT` if your upstream is normally slow. The result is cached for 10 seconds per backend and model. That cache time is fixed in the code (`_MODEL_TTL` in `src/nexus/status.py`) and has no setting.

## Limits

- A subscription backend returns 503 when its CLI is missing or logged out, and 400 for client tool calls. Inline screenshots are accepted only on `codex`, `claude-code` and `copilot`. `grok-build` and `antigravity` return 400 `images_not_supported`. Remote image URLs are not fetched. `max_tokens` is not enforced on those CLIs. The full table is in [CONNECT.md](../CONNECT.md#os3-agent-tools-and-screenshots).
- `/v1/messages` on the non-Anthropic backends translates text, images and parallel tool use, and drops Anthropic prompt-cache controls and extended thinking. With `NEXUS_BACKEND=anthropic` the JSON is forwarded, including thinking and cache controls.
- Subscription streaming is OpenAI SSE built from CLI events. The HTTP status is an error only when the CLI fails before the first token.
- No per-client quotas or billing. `NEXUS_MAX_INFLIGHT` (default 8, at most 64) caps concurrent upstream chat calls. Retries are limited to the optional fallback chain, one re-ask after an invalid tool call, and the one `chatgpt` retry described above.
- SSE secret redaction is per chunk. Non-stream bodies are redacted whole.
- Upstream redirects are refused.
- Image generation is implemented (see Images above). Embeddings are not: `/v1/embeddings` answers 404. `/v1/images/edits` and `/v1/images/variations` answer 501.

Rabbit's UI, the macOS Screen Recording consent dialog and the OpenRouter account balance are outside this repository.
