# Upgrading from os3-bridge

Nexus was called os3-bridge. An install made under the old name keeps working and keeps its old names. A fresh install uses the new ones. Nothing is migrated for you.

## What stayed the same

- `./os3-bridge` is the same program as `./nexus`.
- `python -m os3_bridge.cli run` still starts Nexus. It is the command the service unit runs, for old and new installs alike.
- Port `11435`, and the log files `os3-bridge.out.log` and `os3-bridge.err.log` in the checkout.

## Settings and state files

Settings are `NEXUS_*` (see [`.env.example`](../.env.example)). If a `NEXUS_*` variable is unset, the same suffix under `OS3_BRIDGE_*` is read. When both are set, `NEXUS_*` wins, including when it is empty. `OS3_UI_DIST` is still read as the old name of `NEXUS_UI_DIST`.

A checkout that already has an older state file keeps using it until the `.nexus_*` file of the same kind exists:

| Older file | Replaced by |
| --- | --- |
| `.os3_bridge_api_key` | `.nexus_api_key` |
| `.os3_bridge_backend` | `.nexus_backend` |
| `.os3_bridge_model` | `.nexus_model` |
| `.os3_bridge_roles.json` | `.nexus_roles.json` |
| `.os3_bridge_fallback.json` | `.nexus_fallback.json` |
| `.os3_bridge_usage.json` | `.nexus_usage.json` |
| `.os3_bridge_rabbit_watchdog.json` | `.nexus_rabbit_watchdog.json` |
| `.os3_bridge_rabbit_backup.json` | `.nexus_rabbit_backup.json` |

`./nexus test` prints the key file in use. `./nexus start` passes the backend name from the choice file to the service as both `NEXUS_BACKEND` and `OS3_BRIDGE_BACKEND`.

## Service names and folders

| | Fresh install | Install made under the old name |
| --- | --- | --- |
| macOS LaunchAgent label | `ai.nexus.server` | `com.grokbot.os3-bridge` |
| Linux systemd user unit | `nexus.service` | `os3-bridge.service` |
| Provider keys, macOS | `~/Library/Application Support/Nexus/` | `~/Library/Application Support/GrokBot/os3-setup/` |
| Provider keys, Linux | `~/.config/nexus/` (under `$XDG_CONFIG_HOME` when set) | `~/.config/os3-bridge/` |
| Rabbit Chrome profile, macOS | `~/Library/Application Support/Nexus/os3-chrome-debug` | `~/Library/Application Support/GrokBot/os3-chrome-debug` |
| Rabbit Chrome profile, Linux | `~/.config/nexus/os3-chrome-debug` | `~/.config/os3-bridge/os3-chrome-debug` |

"GrokBot" is only a folder and label name. It is not a backend and has nothing to do with `grok-build` or `xai`.

How the old names are handled:

- If only the old service is installed, `./nexus start`, `stop` and `status` manage it in place and never install a second copy. `./nexus stop` records `.nexus_legacy_service` in the checkout (git-ignored), so a stop-then-start restart, such as the menu bar's **Restart door**, puts it back under the same label.
- A machine with only the old key folder and no new one keeps every key file there, including keys that `./nexus setup` or the dashboard save later, and its Rabbit Chrome profile beside them.
- Once the new folder exists, saves go there. A key that exists only in the old folder is still read from it. If both folders have a file, the new one wins and a warning is logged. Nothing is moved or deleted.
- `NEXUS_SERVICE_LABEL` and the `NEXUS_*_KEY_FILE` and `NEXUS_RABBIT_PROFILE` overrides win over all of this.

To move a service to the new name: `./nexus stop`, delete `.nexus_legacy_service`, then `./nexus start`. Anything that restarts the old label by name must switch. To move keys, move the files (or the profile folder) into the new folder yourself. Do not rename the plist or the folders by hand: `./nexus stop` unloads only the label this checkout resolves.

To uninstall an old-name install, remove the old folders in the table as well (the README lists the new ones).
