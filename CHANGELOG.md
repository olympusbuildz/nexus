# Changelog

## [Unreleased]

### Key files stay in one folder on legacy installs

- A machine with only the legacy key folder (`GrokBot/os3-setup/` or `~/.config/os3-bridge/`) and no Nexus folder keeps every key file there, including keys `setup` or the dashboard save later, and its Rabbit Chrome profile beside them. Before, a new key created the Nexus folder and split the keys across two.
- Once the Nexus folder exists, saves go there, and a key that exists only in the legacy folder is still read from it. A rewritten Nous token follows the same rule.
- `./nexus stop` prints a warning when it cannot write `.nexus_legacy_service`, naming the label the next `start` will use instead.

### Nexus names for new installs; existing installs unchanged

- A fresh install uses the LaunchAgent label `ai.nexus.server`, the systemd unit `nexus.service`, and the folder `~/Library/Application Support/Nexus/` (macOS) or `${XDG_CONFIG_HOME:-~/.config}/nexus/` (Linux) for provider key files and the Rabbit Chrome profile (`os3-chrome-debug`).
- An install that already has the old names keeps them with no action: the LaunchAgent `com.grokbot.os3-bridge` or the unit `os3-bridge.service` is started, stopped and reported in place and never installed a second time; keys and the Chrome profile are read from `GrokBot/os3-setup`, `GrokBot/os3-chrome-debug` or `~/.config/os3-bridge` until the new path has that file. The rule is per path: the new path wins when it exists (a warning is logged if the old one exists too), else the old one is used, else the new one is created.
- `./nexus stop` on a legacy-named service records `.nexus_legacy_service` in the checkout (git-ignored), so the menu bar's Restart door (stop, then start) puts it back under the same label instead of silently re-labelling it.
- Nothing is migrated for you. Moving a legacy service to the new name is `./nexus stop`, delete `.nexus_legacy_service`, `./nexus start` (docs/upgrading-from-os3-bridge.md); keys and the Chrome profile move by hand, and the new path wins as soon as it exists.
- `NEXUS_SERVICE_LABEL` and the `NEXUS_*_KEY_FILE` / `NEXUS_RABBIT_PROFILE` overrides still win over all of this.
- The packaging templates are renamed `ai.nexus.server.plist.template` and `nexus.service` (the pinned hashes in `tests/test_invariants.py` changed on purpose; an already-installed unit is not re-rendered). The commands inside, `os3_bridge.cli run`, and the `os3-bridge.*.log` names are unchanged. The Rabbit watchdog refuses to target `ai.nexus.server` as well as the legacy label.

### Security and CI

- CI runs with read-only token permissions (`permissions: contents: read`), every action is pinned to a full commit SHA, and checkout does not persist credentials.
- Dependabot checks pip, npm (`ui/`) and GitHub Actions weekly. `.github/CODEOWNERS` is new.
- `.gitignore` also covers any other `.nexus_*` or `.os3_bridge_*` local state, backup or temp copy, and `*.pem`.

### Fixed

- `chatgpt`: when opening the upstream request fails with a transient TLS or connection error (an SSL error, a connect error or timeout, or a read or write error), Nexus waits 0.3 to 0.5 seconds and tries once more before any byte reaches the client. An HTTP status from the upstream is an answer and is never retried. If the retry also fails, the client gets the same error as before: 502 `upstream_ssl_error`, or the existing timeout or unreachable error.

### Docs

- README rewritten for a first-time Rabbit R1 owner. Detail moved to `docs/backends.md`, `docs/service.md`, `docs/rabbit-setup.md` and `docs/upgrading-from-os3-bridge.md`; the route list moved to `SECURITY.md` and the project tree to `CONTRIBUTING.md`.
- Corrected: plan CLIs cannot serve OS3 (it sends tools and they refuse them), the provider key locations, which settings reach the packaged service, what `chatgpt` and `nous` read from disk, the `agy` command line, the full list of routes that need no token, and the `make dev-setup` steps.
- `.env.example`, the `pyproject.toml` description and the bug report template list the current settings and backends.
- The bug report template had a `description` that was not valid YAML (an unquoted backtick), so GitHub could not load it. It is quoted now.

## [0.7.2] - 2026-10-18

### ChatGPT plan and TLS

- An `ssl.SSLError` from the upstream (seen live as `SSLV3_ALERT_BAD_RECORD_MAC`) no longer becomes HTTP 500. Before the first byte it is a 502 `upstream_ssl_error`. After the 200 has started, on the chatgpt and the shared API-key streams, it is one logged warning, an SSE error event and `data: [DONE]` (#3, #5).
- The default `chatgpt` model is `gpt-6-luna`, and the built-in list is the gpt-6 family. Models the plan still serves stay requestable (#5).
- `./nexus test` runs the tool round trip on `chatgpt` (#4). With no terminal, `install.sh` prints the non-interactive setup command (#4).
- UI lint is clean (UsagePanel). The gitleaks config allowlists the remaining synthetic test keys (#3, #4).

### Repeated mistakes are checks now

- `make check` and CI run `scripts/check_commit_identity.sh`: every commit in the PR range must have the noreply author and committer, and no personal mailbox address may be in the tree.
- New guards in `tests/test_invariants.py`: one default-port literal (in `config.py`; the dashboard page, hints and the OpenRouter and Nous referer now follow `NEXUS_PORT`), loopback-only bind, no request-scoped `ContextVar` write inside a stream generator, no key or token printed or logged unredacted, pinned launchd and systemd unit templates, one Node major (22) across `ui/.nvmrc`, `engines`, CI and `install.sh`.
- `/dashboard/bridge-key` refusals are one table-driven test over `Origin`, `Sec-Fetch-Site` and `Host`.
- The UI needs Node 22 (`ui/.nvmrc`, `engines`); `install.sh` reads the required major from `ui/.nvmrc`.
- Comments that narrated code or recorded workarounds are gone from `src/`, `tests/`, the Makefile and `ui/src`. Provider quirks stay. `AGENTS.md` pairs each rule with the check that enforces it.
- Test suite: the fake SSE upstream sends a usage chunk only when the request asked for one, and stream usage is off in the suite unless a test turns it on.

### Usage tracker: real provider numbers

- Streamed token counts were lost: a streamed body is sent from a child task, so the counts it noted never reached the request log, and every streamed call was stored with 0 tokens. The log now uses one shared per-request holder, and streamed replies are read for the provider's own `usage` (`response.completed` via the ChatGPT translation, `include_usage` chunks, Messages `message_start` / `message_delta`).
- OpenAI-style backends (Nous, OpenRouter, OpenAI, xAI) are asked for `stream_options.include_usage` on streams. The extra usage-only chunk is dropped unless the client asked for it. `NEXUS_STREAM_USAGE=0` turns this off.
- ChatGPT plan: `/dashboard/usage` also reads `GET /backend-api/wham/usage` (the endpoint behind the Codex CLI's `/status`), cached 60 s, read-only, only with a ChatGPT login on file. Only percents, window lengths, reset times, the plan name, `limit_reached` and the credit flag are kept; the body's user, account and email are never read. `NEXUS_CHATGPT_USAGE_POLL=0` turns it off. A failed read shows a note and keeps the last reading.
- Every real reading carries `from` (`usage endpoint` / `response headers`) and `age_s`. A window that reset since its reading shows as unknown, not as a coloured bar.
- Dashboard: "From provider" and "Estimate" tags, reading age, and how many calls in the last hour reported usage. Requests stay a local count.

### Fresh-install fixes (2)

- `./nexus setup` prints the OS3 CONNECT Base URL with the configured port (`NEXUS_PORT` / `OS3_BRIDGE_PORT`), not always 11435.
- `./nexus stop` on macOS waits (bounded, about 10 s) after `launchctl bootout` until the label's job is gone, so the port is free when it returns. Never signals a process itself.
- README: an automated install must run `./nexus setup --non-interactive` so the bridge key is minted.

### Security

- `GET /dashboard/bridge-key` now requires browser provenance (`Sec-Fetch-Site: same-origin`, or a loopback `Origin` on `NEXUS_PORT` matching `Host`). A request with neither (curl, scripts) gets 403 `provenance_required`, an OpenAI-style error naming the key file. No in-repo caller used the route outside the dashboard; the CLI, installer, doctor and Rabbit setup read the key file.

### Images

- Vision: chat image inputs (`image_url`, `data:` and `https:`) reach the ChatGPT plan as `input_image` under any client model id. The fallback chain skips a backend whose model does not take image input (from `/models` `architecture.input_modalities`) and names it in `fallback.skipped`.
- `POST /v1/images/generations` (bridge key): ChatGPT plan Images endpoint with `gpt-image-2`, falling back to Responses + the hosted `image_generation` tool on 404/unsupported; usage limit maps to 429 `chatgpt_quota`. `response_format: url` returns a `data:` URL.
- Nous images, opt-in only (`NEXUS_ENABLE_NOUS_IMAGES=1`) with a daily cap (`NEXUS_NOUS_IMAGE_DAILY_CAP`, default 10).
- `/v1/models` lists `gpt-image-2` when the plan login is ready. `/v1/images/edits` and `/variations` answer 501.
- Docs: README image example and OS3 image setup via the saved local connection (a custom provider cannot reach loopback).
- Pulse traffic and `/dashboard/usage` count images (1 h / 5 h / 24 h); the usage panel notes they share the plan percent with chat.

### Fresh-install fixes

- `./nexus run`, `start`, `status` and the port-holder report use `NEXUS_PORT` (default 11435) instead of always checking 11435. `NEXUS_SERVICE_LABEL` overrides the macOS LaunchAgent label (default `com.grokbot.os3-bridge`, unchanged).
- `./nexus setup` lists every backend: API keys first, then `chatgpt`, then the plan CLIs (marked "not installed" when missing; Antigravity marked opt-in), with Nous last as advanced. `setup --help` lists `chatgpt`. An empty key now says it is required.
- `./install.sh` builds the React dashboard when the Node major in `ui/.nvmrc` (or newer) is present and says plainly when the built-in page is used instead (`NEXUS_SKIP_UI=1` skips).
- `./doctor.sh` shows the virtualenv's Python version.
- README: Requirements and Uninstall sections, `NEXUS_PORT`, how OS3 reaches the loopback door, current default-model examples.

### Launch polish

- `GET /dashboard/bridge-key` refuses cross-site and same-site requests and a foreign `Origin` (HTTP 403).
- SECURITY.md lists `GET /dashboard/usage`. The CI badge is removed from the README.

### Usage and reset tracker

- `GET /dashboard/usage` and a "Usage and resets" panel on the Status page: percent bars, a live reset countdown, amber at 80% and red at 95%.
- Real numbers only where the provider sends them: ChatGPT's `x-codex-primary/secondary-*` headers (5h and weekly windows), the `x-ratelimit-*` headers (Nous: per-minute and per-hour rate limits, not plan quota), and OpenRouter's `GET /api/v1/key`. Everything else is a local count of requests and tokens per window, labelled as an estimate.
- Kept in `.nexus_usage.json` (gitignored, atomic mode-600 write; `NEXUS_USAGE_FILE` overrides). Opaque headers such as `x-codex-turn-state` and keys are never stored.
- Early switch: when the active backend's real used percent reaches `NEXUS_USAGE_SWITCH_PERCENT` (default 95, `off` disables) and a fallback chain is set, it cools down until its window resets and the next backend serves. The panel shows it.

### `chatgpt` backend: ChatGPT plan with tools and streaming

- New backend `chatgpt` (alias `chatgpt-plan`). It reuses the Codex CLI's ChatGPT sign-in and talks to the Codex Responses endpoint directly, so OpenAI-style tools, real token streaming, images and `/v1/messages` work, which the `codex` CLI backend cannot do. At your own risk: see "ChatGPT plan backend" and "Provider risk" in the README.
- The access token is read from the Codex login file at request time and refreshed in place (file lock, atomic mode-600 write) when it is about to expire or is refused. It is never logged or copied.
- `GET /v1/models` lists the plan's models. A usage limit is `chatgpt_quota` (429) and fails over like any other.

### Settings use `NEXUS_*`

- Documented environment variables are `NEXUS_*`. `OS3_BRIDGE_*` is still read when the new name is unset. A set `NEXUS_*` wins, including when it is empty.
- New state files are `.nexus_backend`, `.nexus_api_key`, `.nexus_model`, `.nexus_roles.json`, `.nexus_fallback.json`, `.nexus_rabbit_watchdog.json`, and `.nexus_rabbit_backup.json`. An existing `.os3_bridge_*` file of the same kind stays the file that is read and written until the new file exists.
- `./nexus` and `./os3-bridge` both run the CLI. `python -m os3_bridge.cli run` remains the command an installed LaunchAgent or systemd unit already uses. The label `com.grokbot.os3-bridge`, the unit name `os3-bridge.service`, and the log files `os3-bridge.out.log` and `os3-bridge.err.log` are unchanged.

### `./nexus test`

- Antigravity is off unless `NEXUS_ENABLE_ANTIGRAVITY=1` (`OS3_BRIDGE_ENABLE_ANTIGRAVITY=1` still counts). When it is the active backend
  and the door reports it off, the test prints SKIP for the backend, chat and tool-call steps
  with that opt-in hint instead of FAIL, the server step still PASSes, and the exit code is 0.

### Settings backend switch takes effect without a restart

- A backend saved on the Settings page is the next request's backend. In-flight
  requests finish on the previous one. The red restart notice is gone for a
  backend switch; `/health` and `/doctor` report the new `backend` and a
  `backend_source` of `"file"`.
- The saved choice file now wins over `OS3_BRIDGE_BACKEND`. That env var (including
  the LaunchAgent default) is only used when nothing is saved. A relaunch keeps
  serving the Settings choice.
- Switching *to* Nous starts the in-process Hermes token refresh if it was not
  already running. If that copy fails, the save still applies and the response
  carries `warning` (the reason, never the token).
- Save will not activate a backend that is not ready (same `/health` check:
  CLI logged in, key present). That is `409 not_ready`; the choice file and the
  model file stay as they were. A key-only save (`activate: false`) is unchanged.
- A backend change keeps the saved model only when the new backend lists that
  id. Otherwise the model resets to that backend's default. `model_note` says
  which happened.
- `OS3_BRIDGE_BACKEND` is only the default when nothing is saved. Deleting
  `.os3_bridge_backend` brings that env pin back. The choice file is written
  with a temp file in the same directory and `os.replace`.

### One name: Nexus

- README, `CONNECT.md`, `SECURITY.md`, `docs/` and the dashboard UI now call the project Nexus
  throughout (formerly os3-bridge). `./nexus` is the documented command; `./os3-bridge` keeps working for existing
  installs. Environment variables and state files are covered under "Settings use `NEXUS_*`" above.
  Unchanged: the `~/.config/os3-bridge/` key directory, the LaunchAgent label `com.grokbot.os3-bridge`, and
  port `11435`. Earlier entries below keep the old name as history.
- Example paths in `docs/ui-api.md` use `/home/<user>/` placeholders instead of a personal home
  directory.

### Set up Rabbit OS3 for you (opt-in)

- Dashboard API for the Settings screen: `GET /dashboard/rabbit` (plan, status, opt-in state,
  free/billed info per model), `POST /dashboard/rabbit/apply` `{model, dry_run}` and
  `POST /dashboard/rabbit/restore`. Same guard as every other dashboard route, same JSON as the
  CLI, every write refused unless `OS3_BRIDGE_RABBIT_SETUP=on`. Documented in
  `docs/ui-api.md` section 8 (`ui_api` stays `1`).
- `./os3-bridge rabbit plan|setup|restore|verify` registers this door in Rabbit OS3 (a Local
  connection: base URL, bridge key, node id, model) instead of pasting them by hand. Off unless
  `OS3_BRIDGE_RABBIT_SETUP=on`; `plan` is read-only and always allowed. It uses Rabbit's
  undocumented web API through a headed Chrome on a dedicated, signed-in profile
  (`OS3_BRIDGE_RABBIT_PROFILE`, `OS3_BRIDGE_RABBIT_CDP_PORT`), DevTools bound to `127.0.0.1`,
  one tab on `https://os3.rabbit.tech`, closed afterwards. It may break whenever Rabbit changes.
- The model must be in the live `/v1/models` list (checked before any delete); non-`:free` models
  warn "may be billed". `--dry-run` prints a before/after diff with the key masked. The previous
  connection is saved (mode 600, no key) and put back if registration fails. When Chrome,
  sign-in or CloudFront stops the automatic path, the command prints the values to paste and
  exits 3. The bridge key is never printed, logged or written to the backup.

### Unknown model ids

- On `POST /v1/chat/completions`, a model id missing from the active backend's live model list
  (for example an OpenRouter id while Nous is active) is served on the active model instead of
  failing upstream. The response `model` is the one served, `X-OS3-Model-Remapped: <requested id>`
  is added only when that happened (streams too), the remap is one INFO log line, and
  `GET /dashboard/pulse` counts `traffic.model_remapped`. An exact trimmed, case-insensitive match
  serves the list's canonical id. No list, subscription CLI backends, role-set models and
  `/v1/messages` are left alone. `OS3_BRIDGE_MODEL_FALLBACK=off` restores pass-through.

### One-command install

- `./install.sh` is the whole first run: `.venv`, backend choice (asked on the terminal when
  there is one), `./os3-bridge start`, then `./os3-bridge test --wait 20`, whose exit code it
  returns. Re-running reuses `.venv` and changes nothing else. No `sudo`, no service restart,
  and a process already on `127.0.0.1:11435` is never touched: when `start` refuses and
  something is listening on the door port, it still runs `./os3-bridge test` (which names the
  holder); when nothing is listening it prints `Next: ./os3-bridge test` and exits with
  `start`'s code. Run from a clone it never updates the clone.
- Piped from curl (`curl -fsSL .../install.sh | bash`) it clones
  into `$NEXUS_DIR` (default `~/nexus`), or, when that is already a checkout, runs
  `git pull --ff-only` (if that fails, e.g. diverged or dirty, it warns and keeps the
  checkout as is; never reset, stash, or force), then runs the checkout's `./install.sh`.

### `./os3-bridge test`

- New command. Prints the values to paste into OS3 or any client (base URL, bridge key file
  path, never the key; active backend and model), then runs one real round trip against the
  local door: server and backend readiness, `/v1/models` through `127.0.0.1` and through
  `127.1` (reported, so you can see which answers), one plain chat, and one tool-call round
  trip with a schema-valid call and a final answer. Each step is PASS, FAIL (with a one-line
  fix), SKIP (subscription CLIs refuse tools) or INFO. Bounded timeouts; exit 1 on a failure.
  `--wait SECONDS` keeps retrying while nothing is listening yet (right after `start`).
  When the port answers but is not os3-bridge, it names the holder.
- `/v1` already ignores the `Host` name, so `http://127.1:11435/v1` works. The dashboard Host
  allowlist is unchanged.

### Port conflicts

- When `127.0.0.1:11435` is taken, `./os3-bridge start`, `run`, `stop` and `./doctor.sh` name the
  holder: pid, a truncated command, and whether it is the packaged LaunchAgent/systemd unit,
  another checkout of os3-bridge, or something else, plus the exact safe next step. The holder is
  never signaled. Linux hosts without `lsof` fall back to `ss`, then `/proc`; the lsof, ss,
  launchctl and systemctl calls now have a timeout.

### Tool-call validation and safe repair

- Tool calls are now checked on `POST /v1/messages` too when the backend is reached
  through chat, not only on chat completions. The native Anthropic backend is still plain
  forwarding. An upstream error chunk now ends a chat-translated `/v1/messages` stream
  with an Anthropic `error` event.
- An answer cut off by the token limit (`finish_reason: length`) is passed through, not
  re-asked or rejected.
- New deterministic repairs: tool name in another case or with stray spaces (never a
  prefix), one trailing
  comma, trailing junk after the arguments object, numeric strings where the schema says
  number or integer, and unknown keys when the schema sets `additionalProperties: false`.
- A call that is still invalid after repair and the one re-ask is no longer passed on.
  The client gets `invalid_tool_call` (HTTP 502, or an error event on a stream). A device
  id that the prompt does not list stays advice for the re-ask and never rejects.
- `GET /dashboard/pulse` `traffic.tool_repair` counts `repaired` and `rejected`.
  `OS3_BRIDGE_TOOL_REPAIR=off` still restores pass-through.

### Cross-provider fallback at a usage limit

- `OS3_BRIDGE_FALLBACK` (or `.os3_bridge_fallback.json`) names an ordered chain such as
  `codex,claude-code,nous,openrouter`. A chat request that fails with `out_of_quota` on the
  active backend is retried on the next present backend before any byte is streamed; the
  exhausted backend cools down for `OS3_BRIDGE_FALLBACK_COOLDOWN` seconds (default 900).
  Off unless a chain names a second backend. Auth errors, bad requests and 5xx never fail over.
- Detection is one table in `errors.py` (`SIGNALS`) read only by the failover. It separates a
  spent quota (long cooldown) from a rate limit (cooldown of `Retry-After`, 60 s without one)
  and from a backend that cannot serve the request (skipped, no cooldown). `Upstream` keeps
  `Retry-After` on the error object; no error body changes.
- Each backend in the chain uses its own model; `OS3_BRIDGE_MODEL` and the client's model
  apply to the active backend only. A tools or image request is never sent to a backend that
  refuses it.
- `X-OS3-Backend` is sent only when another backend answered. `doctor.sh` prints the chain
  and warns when it goes from a subscription backend to a key-billed one.
- The CLI backends' own failure classification is unchanged (pinned by
  `tests/test_cli_failure_golden.py`).
- `GET /health` and `GET /doctor` add `fallback` (chain, active, cooling, `failovers`);
  `GET /dashboard/pulse` adds `fallback` and `traffic.failovers`; a failed-over call's pulse
  entry carries the backend and model that answered. Additive only.

### rabbit-agent watchdog

- Opt-in (`OS3_BRIDGE_RABBIT_WATCHDOG=on`, macOS): restart a crashed `rabbit-agent` LaunchAgent with
  `launchctl kickstart`, at most 3 per hour (kept across restarts in a mode-600 state file). Acts only on
  `state = not running` with a readable crash; 10 second limit per launchctl call. With the flag on,
  `GET /health` and `/doctor` add a `watchdog` object; unset, they are unchanged. `/v1`, the bind and
  the door's own LaunchAgent are unchanged.

### Nexus UI API (`docs/ui-api.md`, `ui_api: 1`)

- `GET /health` and `GET /doctor` add `ui_api: 1` and, on every backend row, `name`, `kind`
  and `state` (`ready`, `not_configured`, `cli_missing`, `not_logged_in`, `out_of_quota`).
  `ready` and the existing fields are unchanged. `out_of_quota` is derived from recent chat
  traffic and clears on the next success or after 15 minutes; it is not a plan balance.
- `GET /dashboard/pulse` entries add `backend`, `error_code` and `hint`.
  `GET /dashboard/models` rows add `backend`.
- `GET /dashboard/config` reports the active backend and model with where each came from
  (`env`, `file`, `default`) and, per provider key, presence, source, path, mode and a
  last-four mask. A key is never returned. `POST /dashboard/config` takes `activate: false`
  to save a verified key without switching the backend or model.
- `POST /dashboard/bench` times one prompt across up to six backend/model targets,
  synchronously and one run at a time (`409 bench_busy`).
- Every error the bridge produces outside `/v1` is `{ok: false, error, code, hint}`;
  `error` is still a string. An unexpected 500 outside `/v1` now uses it too.
- A built UI is served at `/` when `ui/dist/index.html` (or `$OS3_UI_DIST`) exists, with
  single-page-app fallback and the same-origin guard in front. Without one the built-in
  dashboard is unchanged.
- `/v1`, the `127.0.0.1:11435` bind and the packaged service are unchanged;
  `tests/test_v1_unchanged.py` pins the `/v1` bytes.

### Credentials

- `/health` and `/doctor` ask the provider whether the key works. A refused key (401/403)
  is `ready: false` with `upstream_status` and a hint that names the rejection, instead of
  "could not confirm model". OpenRouter's public `/models` list no longer makes a dead key
  look ready: its key is checked with `GET /key`. Role-aware readiness is unchanged, and
  the key is asked about once per poll, not once per role.
- Setup rejects placeholder and provider-refused keys before it writes or keeps them
  (also from the dashboard), and `doctor.sh` fails on one. `OS3_BRIDGE_DOCTOR_SKIP_CRED=1`
  skips the call.
- The Nous token no longer needs an hourly manual sync. `./os3-bridge start` and process
  boot copy a live token from the Hermes auth store (and refuse with a clear message when it
  is missing or expired); the running door refreshes it every 45 minutes.

### Per-role models (OS3)

- `POST /v1/chat/completions` is classified as OS3's main chat, a worker, background
  work, or the image-understanding check, and each can use its own model:
  `OS3_BRIDGE_MODEL_CHAT`, `_WORKER`, `_BACKGROUND`, `_VISION`, or a
  `.os3_bridge_roles.json` file (`OS3_BRIDGE_ROLES_FILE`). The environment wins per
  role; a role with no setting keeps the model OS3 sent. A setting wins over OS3's model
  field, and the log records only the role. The image check uses the vision model, else
  the worker model, else OS3's. Tool repair runs on the routed model.
- `/health` and `/doctor` add `roles`, `roles_ready`, and `roles_hint`. `ready` is false
  when a configured role model is missing on the active backend, cannot be confirmed, or
  is not a valid id. `doctor.sh` and the probe name the failing role. The dashboard shows
  each role's model under Model and reports a failing one.

### Tool calls (OS3)

- Tool calls in `POST /v1/chat/completions` answers are checked against the
  request's own tool schemas and repaired before OS3 sees them: arguments that
  are not valid JSON (stray backslashes, fences, raw newlines, double encoding),
  a device name or garbled id in `node_id`, an action name given as the
  `computer_use` script, a capture with no `feed_image`, a missing call id. A
  call that is still invalid gets one re-ask to the same model. Answers with
  nothing to fix are returned byte for byte, and a failed repair returns the
  upstream answer unchanged. `OS3_BRIDGE_TOOL_REPAIR=off` turns it off.
- Offline OS3 replay harness (`tests/os3_replay.py`, `python -m tests.os3_replay`)
  with chat, worker, and computer-use fixtures and a scripted fake upstream.

### Dashboard

- Traffic through this door: process-local call totals, ok / failed, usage-limit
  hits, p50/p95 latency from the recent ring, and token sums only when an
  upstream response reported them. Explicit copy that this is not a Claude /
  ChatGPT / Grok plan balance. Pulse entries carry optional token counts and a
  usage-limit flag.

### Provider risk

- `antigravity` is off unless `OS3_BRIDGE_ENABLE_ANTIGRAVITY=1`. Off, setup does not offer it,
  chat returns 403 `backend_disabled`, and `/health` and `/doctor` report `ready: false` with a
  hint naming the switch. README gains a Provider risk table and a no-telemetry, no-auto-update
  statement. CI now runs on ubuntu-latest and macos-latest.

## [0.7.1] - 2026-10-04

This release adds the cyan dashboard, the security hardening, the dashboard
polish below, the OS3 pitch in the docs, and the GitHub issue and pull request
templates.

### Dashboard

- Status, the Configure panel, and the tab title read one verdict (Ready,
  Unconfirmed, Not ready, Degraded, Down), so the page no longer says Up while
  the backend is not ready. Configure shows what is running and marks another
  backend as a draft until it is saved.
- A persistent failure banner appears when `/health` is unreachable, the backend
  or model is not ready, or the last chat call failed with a 5xx. Its copy is
  the door's own diagnosis.
- Backends start collapsed to the active backend and a ready count, with a chip
  per backend. Show all expands the full list.
- Model ids say where they came from: live, reported by the CLI, catalog, or
  unverified. Catalog ids are starting guesses, not entitlements.
- New `GET /dashboard/models?backend=` feeds the model picker. It asks the
  provider only when a key is on file, and falls back to the catalog.
- Sonar scope beside the pulse feed. The page stays dark, and Nous Portal is
  listed last and marked advanced.

### Docs

- README names what is in this repo versus sibling products (Rabbit,
  GrokBot paths, Hermes), documents keeping the `com.grokbot.os3-bridge` label,
  and moves Nous Portal out of the lead pitch into an advanced section.
- README and CONNECT lead with the OS3 harness path: Claude / ChatGPT / Grok
  plan (via the official CLI) into OS3 Local. OpenRouter stays the
  setup default when no plan CLI is on `PATH`. Setup prints "OS3 CONNECT".
- `assets/banner.png`, a badge row and feature table in the README, install and
  getting-started sections, `AGENTS.md`, and `.github` issue and pull request
  templates.

### Security

- The unauthenticated dashboard write could be driven by any web page open in a
  local browser: a cross-origin `text/plain` POST to `/dashboard/config` needs no
  CORS preflight and could swap the backend or overwrite the provider key. A
  rebinding hostname was also accepted as `Host`. Routes outside `/v1` now
  require a loopback `Host`, and the dashboard write requires same-origin
  `application/json`.
- The Nous Portal key is no longer passed to subscription CLI child processes.

### Fixed

- A `/health` or `/doctor` call with a cold CLI cache ran the CLI probe on the
  event loop, freezing every request and streaming chat behind it (5.75s with
  one 3s CLI). Probes now run in a worker thread.
- A provider 401 or 403 was reported as "rejected model ... check the model id".
  It now says the key was rejected and how to replace it.
- `./os3-bridge start` on a Linux host with no systemd user session died with a
  raw `Failed to connect to bus`, left a stale unit file behind (so `stop` later
  claimed to have stopped it), and crashed with a traceback when `systemctl` was
  missing. It now explains, suggests `./os3-bridge run`, and cleans up.
- Two callers racing to create the bridge key on first use could be handed
  different keys (280 of 300 trials). Creation is now atomic.
- `os3-bridge setup --backend` help and `CONNECT.md` omitted `nous`.

### Changed

- `requirements.txt` pins the versions the suite was verified against, the empty
  `uv.lock` stub is gone, and CI runs on Python 3.11 and 3.12.
- `SECURITY.md` documents the loopback bind, the rules for the routes without a
  token, the dashboard write rule, provider-key handling, and a private
  reporting path.
- The docs name the shipped default model.

## [0.7.0] - 2026-09-30

First release in this changelog. Everything before it is the build-up.

- OpenAI `/v1` and Anthropic `/v1/messages` front doors on loopback
- API-key backends: OpenRouter (default), Nous Portal, OpenAI, Anthropic, xAI
- Subscription-CLI backends: codex, claude-code, copilot, grok-build, antigravity
- Browser dashboard on `/`, optional macOS menu-bar status, launchd and systemd units
- Dashboard pulse of recent `/v1` calls, client wiring, and an honest restart note
- Dashboard control plane: self-hosted Syne and IBM Plex, one cyan accent, model list for every backend
- `doctor.sh` live readiness check; `/health` reports per-backend login state

### Fixed since the feature work

- Access log crashed on every request: the formatter read `client_addr`,
  `request_line`, and `status_code` as record attributes uvicorn 0.54 does not
  set, flooding stderr with logging tracebacks.
- `doctor.sh` probed the model the caller's env resolved instead of the one
  the running door reported, so a service pinned by launchd or systemd env
  could fail checks it was actually passing.
- The `/v1` catch-all routes no longer emit duplicate-operation-id warnings
  in the OpenAPI schema.
