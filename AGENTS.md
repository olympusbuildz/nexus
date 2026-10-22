# AGENTS.md

Map for coding agents working in this repository. Humans: start with [CONTRIBUTING.md](CONTRIBUTING.md).

## What this repo is

Nexus (`./nexus`; `./os3-bridge` still works) is a local OpenAI-compatible `/v1` endpoint on `127.0.0.1:11435`, built for Rabbit OS3 (OS3 Local) and usable by any app with a custom base URL. Upstream is one backend: an API key, the `chatgpt` plan login, or a subscription CLI. It is not Hermes Agent and not Rabbit’s UI.

## Layout

| Path | Touch when… |
| --- | --- |
| `src/nexus/app.py` | HTTP routes, middleware, process entry |
| `src/nexus/backends/` | A provider or CLI upstream |
| `src/nexus/tool_repair.py` | Tool-call validation and repair on the chat and Messages response paths |
| `src/nexus/setup_flow.py` | Setup wizard / OS3 CONNECT banner |
| `src/nexus/dashboard.py` | Browser control plane on `/` |
| `os3_bridge/` | Compat shim only: keeps `import os3_bridge` and `python -m os3_bridge.cli` (installed units) working |
| `nexus`, `os3-bridge` | Same wrapper script; runs `python -m nexus.cli` with `src/` on `PYTHONPATH` |
| `packaging/` | launchd / systemd unit templates |
| `CONNECT.md` | Client paste-in contract |
| `tests/` | Behavior change — keep `make check` green |
| `tests/os3_replay.py` | OS3-shaped replay harness; `python -m tests.os3_replay` prints the before/after table |

## Rules, and the check that enforces each

A rule without a check below is a judgment call. `make check` runs every check here; a new mistake gets a new row, not a new paragraph.

| Rule | Enforced by |
| --- | --- |
| Every commit's author and committer is `olympusbuildz <314062777+olympusbuildz@users.noreply.github.com>`; no personal mailbox address in the tree | `scripts/check_commit_identity.sh` (`make identity`, CI on the PR range); `tests/test_commit_identity_sh.py` |
| Bind `127.0.0.1` only; widening the bind is a bug | `tests/test_invariants.py::test_main_refuses_every_non_loopback_host`, `::test_the_default_bind_is_loopback`, `::test_uvicorn_starts_in_one_place_with_the_loopback_host`, `::test_no_wildcard_bind_address_is_written_anywhere_in_the_package_or_units` |
| The default port is spelled once, in `config.py`; everything else reads `NEXUS_PORT` | `tests/test_invariants.py::test_the_default_port_literal_lives_in_config_only`, `::test_default_port_constant_is_not_used_outside_config`; `tests/test_port_config.py::test_door_addresses_in_the_page_and_hints_follow_nexus_port` |
| `GET /dashboard/bridge-key` needs same-origin browser provenance (no, foreign or cross-site `Origin`, `Sec-Fetch-Site` or `Host` is refused) | `tests/test_guard.py::test_bridge_key_is_refused_without_same_origin_provenance` |
| No key, token or secret reaches a log, print or exception text unredacted | `tests/test_invariants.py::test_nothing_secret_is_logged_or_printed_unredacted`; `tests/test_secrets.py::test_no_key_reaches_the_logs_or_the_terminal`, `::test_chatgpt_credentials_never_show_their_token` |
| Upstream and DevTools HTTP clients ignore proxy variables and redirects | `tests/test_secrets.py::test_upstream_and_devtools_clients_ignore_proxy_env_and_redirects` |
| Request-scoped state is never written from a stream generator; streamed token counts reach the request log | `tests/test_invariants.py::test_no_context_var_write_inside_a_stream_generator`; `tests/test_usage_real.py::test_streamed_chat_records_provider_tokens` |
| An empty `response.completed` output never drops streamed tool calls | `tests/test_chatgpt.py::test_empty_final_output_keeps_streamed_tool_call_and_text`, `::test_empty_final_output_through_the_door` |
| `codex exec` gets its approval policy as `-c approval_policy="never"`, never `-a` | `tests/test_subscription.py::test_codex_probe_and_chat` |
| The launchd and systemd unit templates change only on purpose | `tests/test_invariants.py::test_installed_unit_templates_are_unchanged_without_intent` |
| One Node major for the UI: `ui/.nvmrc`, `engines`, `@types/node`, CI and `install.sh` agree | `tests/test_invariants.py::test_node_major_agrees_across_nvmrc_engines_and_ci` |
| Fresh installs use the Nexus names (`ai.nexus.server`, `nexus.service`, the `Nexus` / `nexus` key folders). An install on the pre-Nexus names (listed in `docs/upgrading-from-os3-bridge.md`) keeps them: managed in place, never moved or relabelled automatically, and a stop-then-start never changes the label | `tests/test_legacy_names.py::test_fresh_machine_gets_the_nexus_names`, `::test_legacy_only_launchd_machine_keeps_its_label_and_is_left_running`, `::test_stop_then_start_on_a_legacy_machine_never_changes_the_label`, `::test_both_installed_new_wins_with_a_warning_and_the_legacy_one_is_untouched` |
| `/health` and `/doctor` report `ready` only when chat would work | `tests/test_credential_health.py::test_dead_openrouter_key_is_not_ready_even_though_models_lists`; `tests/test_model_readiness.py::test_health_is_not_ready_and_names_the_model` |
| Error bodies carry a stranger-usable `hint` | `tests/test_stream_done.py::test_auth_and_body_errors_carry_hint`; `tests/test_nexus_api.py::test_guard_refusals_carry_code_and_hint` |
| Every row above names a test that exists | `tests/test_invariants.py::test_every_check_named_in_agents_md_exists` |
| The suite and ruff stay green | `make check`, same gate as CI |

Judgment calls, kept as prose:

- Prefer small diffs. One idea per PR.
- No secrets in issues, screenshots or commit messages: look before you attach.
- Comments only for a provider quirk or a constraint the code cannot say. Put a rule in a type, an API or a check instead.

## Check

```bash
make dev-setup                       # once: .venv plus dev tools (uses uv; see CONTRIBUTING.md)
. .venv/bin/activate && make check   # ruff + commit identity + pytest, same gate as CI
```

## Docs pitch

Lead with what works for the reader. Rabbit OS3 sends tool definitions on its main requests, and every plan CLI refuses them, so an OS3 owner needs an API key or the `chatgpt` backend. Say the provider-terms risk of using a subscription near the top. Call the product Nexus.
