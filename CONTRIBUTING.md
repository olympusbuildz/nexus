# Contributing

Small diffs beat large ones. One idea per pull request. Users should start with the [README](README.md).

## Set up and check

`make dev-setup` creates `.venv` with [uv](https://docs.astral.sh/uv/) and installs the dev tools. Without uv, run `python3 -m venv .venv` first and `make dev-setup` installs into it with pip. Python 3.11 or newer is required.

```bash
make dev-setup                       # once
. .venv/bin/activate && make check   # the gate CI runs
```

`make check` runs `ruff check` and `ruff format --check`, then `scripts/check_commit_identity.sh` (every commit in the range needs the project noreply author and committer, and no personal mailbox address may be in the tree), then `pytest -q`. Activate the virtual environment first: `make check` uses whichever `python3` is on your `PATH`. The tests mock upstream HTTP and fake the CLI binaries, make no network calls and do not bind port 11435.

`scripts/local-ci.sh [--no-post] [SHA]` is an optional maintainer tool that runs the same check in a throwaway worktree and posts a commit status. Posting needs `gh auth login` with a token that can write commit statuses.

## Layout

The package is `nexus`, in `src/nexus`. `./nexus` is the command; `./os3-bridge` is the same script, kept for existing installs. The root `os3_bridge/` folder is only a compatibility shim.

```text
src/nexus/     app.py (routes), cli.py, setup_flow.py, dashboard.py, tool_repair.py, backends/
ui/            optional React dashboard, built into ui/dist by install.sh
os3_bridge/    compatibility shim for `python -m os3_bridge.cli`
packaging/     LaunchAgent and systemd unit templates
scripts/       check_commit_identity.sh, local-ci.sh, sync-nous-token.py
tests/         pytest, mocked upstream
docs/          backends, service, rabbit-setup, ui-api (developer reference), architecture diagram
```

[AGENTS.md](AGENTS.md) maps each file to when you should touch it.

## Rules that are not negotiable

- No secrets in code, tests, issues or screenshots. Keys live in mode-600 files, and the tests fail if a secret leaks into a report.
- Nexus binds `127.0.0.1` only. Code that widens the bind is a bug.
- `/health` and `/doctor` report a backend as `ready` only when a chat request would work.
- Error bodies carry a `hint` a stranger can act on.
- Every commit is authored and committed as `olympusbuildz <314062777+olympusbuildz@users.noreply.github.com>`.

[AGENTS.md](AGENTS.md) lists each rule next to the test that enforces it. Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
