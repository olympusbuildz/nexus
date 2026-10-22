# Rabbit OS3 automatic setup

Optional, and off by default. The manual paste in the [README](../README.md#connect-rabbit-os3) always works. The `rabbit` commands do the same paste for you.

Read this first:

- This uses Rabbit's undocumented web API, with your own signed-in session, in a dedicated Chrome profile that you sign in to OS3 once. It can break whenever Rabbit changes its site. It is a convenience, not a supported integration.
- It changes settings on Rabbit's servers. Whatever you save in OS3 as the connection, including the Nexus key, is stored there; OS3 reads the key back only in masked form.
- It makes network calls to `os3.rabbit.tech`, and only while you run a `./nexus rabbit` command (or press the matching dashboard button). Nothing else in Nexus contacts Rabbit.
- Nothing below runs unless `NEXUS_RABBIT_SETUP=on` is set.

```bash
./nexus rabbit plan                            # read-only: what would be set, and what it costs
export NEXUS_RABBIT_SETUP=on                   # nothing below runs without this
./nexus rabbit setup --dry-run                 # show the change, make none
./nexus rabbit setup [--model <id>]            # delete + register a Local connection
./nexus rabbit verify                          # does OS3 point at Nexus, did it call us
./nexus rabbit restore                         # put back the connection saved before setup
```

The CLI reads `NEXUS_RABBIT_SETUP` from your shell. The dashboard routes (`POST /dashboard/rabbit/apply` and `/restore`) read it from the process that serves the dashboard, and the packaged service does not receive it (see [service.md](service.md#what-the-service-receives)). To use the dashboard buttons, run `NEXUS_RABBIT_SETUP=on ./nexus run` instead of the service. Those routes need no token; see [SECURITY.md](../SECURITY.md#routes).

## How it behaves

- The values come from this machine, not from you: the Nexus key file, the node id in `~/.rabbit-agent/connection.json`, `http://127.0.0.1:11435/v1`, and a model from Nexus's live `/v1/models` list. An id that is not in that list is refused before anything is changed, with the closest real ids named. A model not ending in `:free` gets a "may be billed" warning.
- Chrome is headed and bound to `127.0.0.1` (default port `9333`; override with `NEXUS_RABBIT_CDP_PORT`). The profile is `~/Library/Application Support/Nexus/os3-chrome-debug` on macOS and `~/.config/nexus/os3-chrome-debug` on Linux (override with `NEXUS_RABBIT_PROFILE`). Sign in to `https://os3.rabbit.tech` once in that profile. Only one new tab is opened, only on `https://os3.rabbit.tech`, and it is closed afterwards. Headless Chrome is blocked by CloudFront, and that is not worked around.
- The previous connection is saved to `.nexus_rabbit_backup.json` (mode 600, masked key only) before anything is deleted. If the new registration fails, the old one is put back when that is possible (a Local one on this Nexus, or another provider whose key file you name in `NEXUS_RABBIT_RESTORE_KEY_FILE`), and the error says whether it worked.
- OS3 only accepts a Local model that Nexus lists. If the current connection uses one Nexus does not list (a stealth model, say), `setup` refuses before deleting anything and prints the paste-in values; `--accept-no-rollback` goes ahead without that safety net. `restore` makes the same check first and never deletes the current connection unless the saved one can be registered.
- If Chrome cannot be started, you are signed out, or CloudFront blocks the page, the command prints the exact values to paste (the key as a file path, never its value) and exits `3`.

## Restoring a non-Nexus provider

OS3 reports only a masked key (a visible prefix, `****`, then the last four characters), so putting back a different provider needs its key file.

When `NEXUS_RABBIT_RESTORE_KEY_FILE` is unset, setup looks for the key file itself. Exactly one file whose key fits the mask (same visible prefix and suffix) is used. Auto-detect needs a mask that shows at least 3 characters before the `****` and 4 after it; a shorter mask picks nothing and the warning says so.

- Where it looks: the `rabbit-keys/` folder next to the Nexus key file (the directory itself is not searched, since in a checkout that is the repo root), and every directory in `NEXUS_RABBIT_KEY_DIRS`, separated like `PATH`, for example `export NEXUS_RABBIT_KEY_DIRS="$HOME/saved-keys"` for a folder where you keep provider keys, one per file. The default `rabbit-keys/` folder is git-ignored, but a directory you add through `NEXUS_RABBIT_KEY_DIRS` is not.
- What a file must be, whether found or named in `NEXUS_RABBIT_RESTORE_KEY_FILE`: a regular file of at most 4 KB directly in the directory (no recursion), never a symlink, owner-only (`chmod 600`, no group or other bits), holding exactly one key on one line and nothing else (no comment line). A skipped file is named in the warning.
- No match keeps the "not restorable" warning. Several matches pick nothing and list the paths. A match is shown as `restore key: auto-detected <path>`. The key itself is never printed, logged or saved.
- `rabbit plan` cannot know OS3's key, so it only counts the candidate files.

## rabbit-agent watchdog (macOS)

A separate option from setup: see [CONNECT.md](../CONNECT.md#rabbit-agent-watchdog-macos-optional).
