#!/usr/bin/env bash
# Readiness check. Prints paths and flags only — never key file contents.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Print a bridge setting. NEXUS_<suffix> wins when it is set, even if empty.
# Otherwise OS3_BRIDGE_<suffix>. Prints nothing when neither is set.
pick_value() {
  case "$1" in
    *[!A-Za-z0-9_]*) return 1 ;;
  esac
  eval "if [[ -n \${NEXUS_$1+x} ]]; then printf '%s' \"\${NEXUS_$1}\"; elif [[ -n \${OS3_BRIDGE_$1+x} ]]; then printf '%s' \"\${OS3_BRIDGE_$1}\"; fi"
}

echo "=== nexus doctor ==="
echo "path: $ROOT"
if [[ -n "${NEXUS_BACKEND+x}" || -n "${OS3_BRIDGE_BACKEND+x}" ]]; then
  if [[ -n "${NEXUS_BACKEND+x}" ]]; then
    backend_env="${NEXUS_BACKEND}"
  else
    backend_env="${OS3_BRIDGE_BACKEND}"
  fi
  if [[ -z "${backend_env}" ]]; then
    echo "backend env: (empty)"
  else
    echo "backend env: ${backend_env}"
  fi
else
  echo "backend env: unset"
fi
if [[ -n "${NEXUS_BACKEND_FILE:-}" ]]; then
  choice_file="${NEXUS_BACKEND_FILE}"
elif [[ -n "${OS3_BRIDGE_BACKEND_FILE:-}" ]]; then
  choice_file="${OS3_BRIDGE_BACKEND_FILE}"
elif [[ -f "${ROOT}/.nexus_backend" || ! -e "${ROOT}/.os3_bridge_backend" ]]; then
  choice_file="${ROOT}/.nexus_backend"
else
  choice_file="${ROOT}/.os3_bridge_backend"
fi
echo "choice file: ${choice_file}"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 missing"
  exit 1
fi
if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  echo "-- Python (venv) --"
  python3 --version
  echo "-- deps (venv) --"
else
  echo "-- Python --"
  python3 --version
  echo "WARN: .venv missing"
  echo "  ./install.sh"
fi

if ! python3 -c "import fastapi, uvicorn, httpx" >/dev/null 2>&1; then
  echo "FAIL: fastapi, uvicorn, or httpx is not importable"
  exit 1
fi
echo "fastapi+uvicorn+httpx OK"

export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

BIND_PORT="$(python3 -c 'from nexus.config import configured_port; print(configured_port())')"

# `start` returns before the door binds, so a doctor run right after a restart can
# read a door that is still coming up as a failure. Poll /health a bounded number
# of times and report the state the last attempt actually saw. The live probe
# below stays the authority on pass or fail; this only stops the race. The wait
# runs before the status probe so the whole retry window sits at the front of
# the run, where it races the bind rather than a diagnostic that takes seconds.
HEALTH_ATTEMPTS="$(pick_value DOCTOR_HEALTH_ATTEMPTS || true)"
HEALTH_INTERVAL="$(pick_value DOCTOR_HEALTH_INTERVAL || true)"
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-5}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-0.5}"

echo
echo "-- wait /health on 127.0.0.1:${BIND_PORT} (${HEALTH_ATTEMPTS} attempts) --"
health_state=0
python3 - "${BIND_PORT}" "${HEALTH_ATTEMPTS}" "${HEALTH_INTERVAL}" <<'PY' || health_state=$?
import sys
import time

import httpx


def _int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except ValueError:
        return default


def _float(raw: str, default: float) -> float:
    try:
        return float(raw)
    except ValueError:
        return default


port = sys.argv[1]
attempts = max(1, _int(sys.argv[2], 5))
interval = max(0.0, _float(sys.argv[3], 0.5))
url = f"http://127.0.0.1:{port}/health"
last = "no answer"

with httpx.Client(trust_env=False, timeout=2.0) as client:
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        else:
            last = f"HTTP {response.status_code}"
            if response.status_code == 200:
                print(f"attempt {attempt}/{attempts}: {last}")
                break
        print(f"attempt {attempt}/{attempts}: {last}")
        if attempt < attempts:
            time.sleep(interval)
    else:
        print(f"no /health answer after {attempts} attempts: {last}")
        raise SystemExit(1)
PY
if [[ "${health_state}" -eq 0 ]]; then
  echo "door is answering"
else
  echo "no answer during the wait; the live check below reports the real state"
fi

echo
echo "-- status --"
status_state=0
python3 - <<'PY' || status_state=$?
import asyncio
import json
import sys

from nexus.backends.registry import build_backend
from nexus.status import doctor_payload

try:
    payload = asyncio.run(doctor_payload(backend=build_backend()))
except Exception as exc:  # noqa: BLE001 - doctor must report, not traceback
    print(f"FAIL: status could not be read: {type(exc).__name__}: {exc}")
    raise SystemExit(1)

print(json.dumps(payload, indent=2))
print()
fallback = payload.get("fallback") or {}
if fallback.get("enabled"):
    print(
        "fallback chain: " + " -> ".join(fallback["chain"])
        + f" (active: {fallback['active']}, cooldown {fallback['cooldown_s']:.0f}s)"
    )
    for warning in fallback.get("warnings", []):
        print(f"WARNING: {warning}")
for problem in fallback.get("problems", []):
    print(f"fallback config problem: {problem}")
if fallback.get("enabled") or fallback.get("problems"):
    print()
if payload.get("ready") is not True:
    # The line a first-time configurer actually needs: what breaks, and what to do.
    if payload.get("model_ready") is True and payload.get("roles_hint"):
        print("NOT READY: a per-role model is not usable.")
        print(f"  {payload.get('roles_hint')}")
        raise SystemExit(3)
    print(f"NOT READY: the bridge would send model '{payload.get('model')}', and it is not usable.")
    print(f"  {payload.get('model_hint')}")
    raise SystemExit(3)
print(f"model that will be sent: {payload.get('model')} (reachable)")
PY

echo
echo "-- provider credential --"
cred_state=0
if [[ "$(pick_value DOCTOR_SKIP_CRED || true)" == "1" ]]; then
  echo "credential check skipped (NEXUS_DOCTOR_SKIP_CRED=1)"
else
  # A key file existing is not proof the key works, and /health's readiness
  # probe never asks the upstream to vouch for it: OpenRouter's /models list
  # answers 200 to a garbage key. One authenticated call decides here, so a
  # placeholder or a refused credential fails the run instead of reading as a
  # healthy door.
  python3 - <<'PY' || cred_state=$?
from nexus.setup_flow import provider_credential_report

code, lines = provider_credential_report()
for line in lines:
    print(line)
raise SystemExit(code)
PY
fi

echo
echo "-- listen 127.0.0.1:${BIND_PORT} --"
python3 -m nexus.port_holder "${BIND_PORT}" || echo "could not name the listener"

echo
if [[ "$(pick_value DOCTOR_SKIP_SMOKE || true)" == "1" ]]; then
  echo "smoke skipped"
  echo "=== doctor done ==="
  exit "${cred_state}"
fi

echo "-- live http://127.0.0.1:${BIND_PORT} (health, models, chat) --"
set +e
python3 -m nexus.probe
probe_code=$?
set -e
echo "=== doctor done ==="
# The probe is the door's verdict. When the door answers but the configured
# model is unusable, that is a louder failure than the door alone, so let the
# status check have the last word. A rejected provider credential fails the
# run either way, door or no door.
if [[ "${cred_state}" -ne 0 ]]; then
  exit "${cred_state}"
fi
if [[ "${probe_code}" -eq 0 && "${status_state}" -ne 0 ]]; then
  exit "${status_state}"
fi
exit "${probe_code}"
