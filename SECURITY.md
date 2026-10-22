# Security

## What this is

Nexus is a local gateway. It listens only on `127.0.0.1:11435`. `./nexus start` and `./nexus run` refuse any other bind address. It is not a service to expose on a LAN, behind a reverse proxy, or in a container with a published port. That bind is the reason the routes below can skip a token: anything that can reach them is already on this machine.

## Routes

| Route | Token | What it does |
| --- | --- | --- |
| `/v1/*` (chat, messages, models, `POST /v1/images/generations`; edits and variations answer 501) | Nexus key, as `Authorization: Bearer` | The OpenAI-compatible API |
| `GET /`, `GET /dashboard/fonts/*` | None | The dashboard page and its fonts, or the built React UI and its files |
| `GET /health`, `GET /doctor` | None | Read-only status |
| `GET /dashboard/pulse` | None | Last 40 `/v1` calls and traffic totals |
| `GET /dashboard/usage` | None | Per-backend usage and reset times |
| `GET /dashboard/models` | None | Model ids for one backend |
| `GET /dashboard/config` | None | Active backend and model; per provider key: presence, path, mode, last-four mask. Never a key |
| `GET /dashboard/bridge-key` | None, same-origin browser only | The Nexus key, for the Copy key button |
| `POST /dashboard/config` | None, same-origin JSON only | Writes a provider key file and the backend and model choice |
| `POST /dashboard/bench` | None, same-origin JSON only | Times one prompt on up to six backend and model targets |
| `GET /dashboard/rabbit` | None | Read-only plan for pointing Rabbit OS3 at Nexus |
| `POST /dashboard/rabbit/apply`, `POST /dashboard/rabbit/restore` | None, same-origin JSON only | Change or restore your Rabbit OS3 connection on Rabbit's servers |

The two Rabbit writes are refused unless `NEXUS_RABBIT_SETUP` is on in the process that serves them. It is off by default and the packaged service does not receive it, so see [docs/rabbit-setup.md](docs/rabbit-setup.md).

A web page open in a browser on this machine can also reach loopback, and a hostile DNS name can point at `127.0.0.1` (DNS rebinding). So every route outside `/v1` carries these rules:

- **Host check.** `Host` must be `127.0.0.1`, `localhost` or `[::1]`. Any other `Host` gets HTTP 403.
- **Writes.** Every `POST` outside `/v1` must be same-origin `application/json`. A foreign `Origin`, a cross-site `Sec-Fetch-Site` or any other content type gets HTTP 403. A cross-origin page cannot send `application/json` without a CORS preflight, and Nexus never answers one.
- **Nexus key read.** `GET /dashboard/bridge-key` is served only to the dashboard in a browser: the request must carry `Sec-Fetch-Site: same-origin`, or an `Origin` of `http://127.0.0.1:<NEXUS_PORT>` or `http://localhost:<NEXUS_PORT>` that matches `Host`. Anything else gets HTTP 403, including a request with no browser provenance such as `curl`. That 403 is an OpenAI-style error (`permission_error`, code `provenance_required`) that names the key file. Non-browser tools read the key file directly (owner-only, mode 600); the CLI, installer, doctor and Rabbit setup already do.

`curl`, the OpenAI SDK, `doctor.sh` and the menu bar send no `Origin` and are unaffected. Key files are mode `600`; do not loosen that.

## What Nexus reads from your machine

- **ChatGPT plan login.** The `chatgpt` backend reads the Codex login file (`~/.codex/auth.json`, or under `CODEX_HOME`), refreshes the token at `auth.openai.com` when it is close to expiry, and writes the new tokens back to the same file with mode 600. It creates a lock file `auth.json.nexus.lock` beside it, and it reads your plan usage from `chatgpt.com`.
- **Nous token.** If you choose the advanced `nous` backend, Nexus copies the token from `~/.hermes/auth.json` into a mode-600 key file and refreshes that copy every 45 minutes.
- **Rabbit OS3.** The opt-in `./nexus rabbit` commands read the node id from `~/.rabbit-agent/connection.json` and contact `os3.rabbit.tech` through a dedicated Chrome profile. Nothing else in Nexus contacts Rabbit.
- **Subscription CLIs.** `codex`, `claude-code`, `copilot`, `grok-build` and `antigravity` use the login the official CLI already holds. Nexus does not read or store it, and the dashboard refuses a key for those backends.

Nexus reads no other login files. Tokens are never logged, copied into responses or shown in the dashboard.

## Provider keys

- Never paste a provider key or the Nexus key (`.nexus_api_key`) into a GitHub issue, pull request, discussion, log or screenshot.
- The dashboard has an empty key field and never shows a key back. The page, `/health`, `/doctor` and `/dashboard/pulse` carry no key material. `/dashboard/bridge-key` returns the Nexus key only, and never a provider key.
- Provider terms are covered in [docs/backends.md](docs/backends.md#provider-risk).
- If a key leaks, revoke it at the provider first, then run `./nexus setup` to write the new one. To replace the Nexus key, rewrite the key file that `./nexus test` prints (`.nexus_api_key` on a new install) as one line, mode `600`.
- OS3 keeps its own copy of the Nexus key on Rabbit's servers. After you replace the key file, update the key in OS3 too.

## Reporting a vulnerability

Do not open a public issue or pull request for a security problem.

1. Preferred: open a private advisory at <https://github.com/olympusbuildz/nexus/security/advisories/new> ("Report a vulnerability" on the repository's Security tab).
2. Otherwise: contact the repository owner, `@olympusbuildz`, through the contact details on the GitHub profile. Ask for a private channel. Put no detail in the first message.

Say what you found, the version (`__version__` in `src/nexus/__init__.py`, or the commit), and the smallest steps to reproduce it. Do not include a real key. You should hear back within a few days. The fix is released before the details are published.

Supported version: the latest tagged release and `main`.
