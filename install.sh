#!/usr/bin/env bash
# One-command install for macOS and Linux. From a clone: ./install.sh
# Without one:
#   curl -fsSL https://raw.githubusercontent.com/olympusbuildz/nexus/main/install.sh | bash
#
# Idempotent. Never uses sudo and never touches a listener already on 127.0.0.1:11435
# (`./nexus start` refuses and names it). Running ./install.sh from a clone never
# updates it. Only the curl form does: into an existing checkout it runs
# `git pull --ff-only` (a failure there warns and keeps the checkout as it is; nothing is
# reset, stashed, or forced), then runs that checkout's ./install.sh.
# Ends by pointing at `./nexus test`, and runs it when the door is up; its exit code
# is this script's. `./os3-bridge` is the same CLI; this script calls `./nexus`.
set -euo pipefail

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

SELF="${BASH_SOURCE[0]:-}"
if [[ -n "${SELF}" && -f "$(dirname "${SELF}")/os3_bridge/__init__.py" ]]; then
  ROOT="$(cd "$(dirname "${SELF}")" && pwd)"
else
  # Piped from curl: there is no checkout beside this script. Clone once, or fast-forward
  # an existing clone. stdin is the script itself, so git must not read it.
  ROOT="${NEXUS_DIR:-${HOME}/nexus}"
  command -v git >/dev/null 2>&1 || die "git is required to fetch Nexus. Install git, or clone the repo and run ./install.sh."
  if [[ -f "${ROOT}/os3_bridge/__init__.py" ]]; then
    if [[ -e "${ROOT}/.git" ]] && GIT_TERMINAL_PROMPT=0 git -C "${ROOT}" pull --ff-only --quiet </dev/null; then
      say "Updated ${ROOT} (fast-forward)."
    else
      say "Warning: could not fast-forward ${ROOT} (diverged, local changes, offline, or not a git clone). Continuing with the checkout as it is." >&2
    fi
  else
    if [[ -e "${ROOT}" && -n "$(ls -A "${ROOT}" 2>/dev/null)" ]]; then
      die "${ROOT} exists and is not a Nexus checkout. Set NEXUS_DIR to an empty or new directory."
    fi
    git clone --depth 1 "${NEXUS_REPO_URL:-https://github.com/olympusbuildz/nexus.git}" "${ROOT}" </dev/null
  fi
  exec bash "${ROOT}/install.sh" "$@"
fi
cd "${ROOT}"

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "${candidate}" >/dev/null 2>&1 &&
    "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="${candidate}"
    break
  fi
done
if [[ -z "${PY}" ]]; then
  die "Python 3.11 or newer is required. macOS: brew install python. Debian/Ubuntu: apt install python3 python3-venv."
fi

# A .venv left by a failed `venv` run (python3-venv missing) has a python but no pip: recreate it.
if .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null &&
  .venv/bin/python -m pip --version >/dev/null 2>&1; then
  say "Reusing .venv"
else
  "${PY}" -m venv --clear .venv || die "Could not create .venv. Debian/Ubuntu: apt install python3-venv, then re-run."
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
say "Installed into ${ROOT}/.venv"

# The React dashboard is optional. Build it when the Node in ui/.nvmrc (or newer) is here; otherwise the
# built-in page on / is served. NEXUS_SKIP_UI=1 skips the build.
if [[ "${NEXUS_SKIP_UI:-}" != "1" && -f ui/package.json ]]; then
  node_need="$(tr -dc '0-9' < ui/.nvmrc)"
  node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
  if [[ "${node_major}" =~ ^[0-9]+$ ]] && (( node_major >= node_need )) && command -v npm >/dev/null 2>&1; then
    say "Building the dashboard UI (Node ${node_major})..."
    if (cd ui && npm ci --silent --no-audit --no-fund && npm run --silent build) >/dev/null 2>&1; then
      say "Dashboard UI built into ui/dist"
    else
      say "Warning: the dashboard UI build failed; the built-in page is served on / instead." >&2
      say "  Retry by hand: cd ui && npm ci && npm run build" >&2
    fi
  else
    say "Node ${node_need}+ not found: the built-in dashboard page is served on /."
    say "  For the full UI later: install Node ${node_need}+, then cd ui && npm ci && npm run build"
  fi
fi

# Same rule as nexus.config.state_path: NEXUS_BACKEND_FILE, else OS3_BRIDGE_BACKEND_FILE,
# else .nexus_backend when it exists or the legacy file does not, else .os3_bridge_backend.
if [[ -n "${NEXUS_BACKEND_FILE:-}" ]]; then
  BACKEND_FILE="${NEXUS_BACKEND_FILE}"
elif [[ -n "${OS3_BRIDGE_BACKEND_FILE:-}" ]]; then
  BACKEND_FILE="${OS3_BRIDGE_BACKEND_FILE}"
elif [[ -f "${ROOT}/.nexus_backend" || ! -e "${ROOT}/.os3_bridge_backend" ]]; then
  BACKEND_FILE="${ROOT}/.nexus_backend"
else
  BACKEND_FILE="${ROOT}/.os3_bridge_backend"
fi
if [[ ! -s "${BACKEND_FILE}" ]]; then
  if (: </dev/tty) 2>/dev/null; then
    ./nexus setup </dev/tty
  else
    say
    say "No backend is chosen yet and there is no terminal to ask in. Next:"
    say "  cd ${ROOT} && ./nexus setup --backend <id> --non-interactive && ./nexus start && ./nexus test"
    say "  An API-key backend reads its key on stdin: add --api-key-stdin. ./nexus setup --help lists the ids."
    exit 0
  fi
fi

# NEXUS_PORT wins when set, even if empty. Else OS3_BRIDGE_PORT, else 11435.
if [[ -n "${NEXUS_PORT+x}" ]]; then
  DOOR_PORT="${NEXUS_PORT}"
elif [[ -n "${OS3_BRIDGE_PORT+x}" ]]; then
  DOOR_PORT="${OS3_BRIDGE_PORT}"
else
  DOOR_PORT=11435
fi

door_listening() {
  "${ROOT}/.venv/bin/python" - "${DOOR_PORT}" <<'PY' 2>/dev/null
import socket
import sys

try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()
except (OSError, ValueError):
    raise SystemExit(1)
PY
}

if ./nexus start; then
  say
  exec ./nexus test --wait 20
else
  rc=$?
fi
if door_listening; then
  say
  say "start did not change anything; something is already on the door port. The test names it:"
  exec ./nexus test
fi
say
say "Nexus did not start. Fix what is printed above, then ./nexus start (or ./nexus run in a terminal), then ./nexus test."
exit "${rc}"
