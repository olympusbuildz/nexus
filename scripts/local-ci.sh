#!/usr/bin/env bash
# Run `make check` for one commit in a throwaway worktree and post a GitHub
# commit status (context local-ci/make-check).
#
# The invoking checkout is never the test tree: it may be dirty, and it may be
# the live door. Make runs only inside the temporary worktree. This script does
# not touch launchd, systemd, or the door's port.
#
# macOS bash 3.2 and BSD mktemp/sed: no associative arrays, no ${var,,}.
set -euo pipefail

# Follow the checkout we were started in, not a caller-exported GIT_DIR.
unset GIT_DIR GIT_WORK_TREE
unset MAKEFLAGS MFLAGS MAKELEVEL

post=1
sha_arg=""
root=""
sha=""
repo=""
tmp=""
wt=""
log=""
py=""
posted_pending=0
final_attempted=0

usage() {
  cat <<'EOF'
Usage: scripts/local-ci.sh [--no-post] [SHA]

Run make check in a throwaway worktree of SHA (default HEAD) and post a
commit status with context local-ci/make-check.

  --no-post   run the check without posting a status
  -h, --help  show this help
EOF
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  set +e
  if [[ -n "$wt" ]]; then
    git worktree remove --force "$wt" >/dev/null 2>&1 || true
  fi
  if [[ -n "$tmp" ]]; then
    git worktree prune >/dev/null 2>&1 || true
    case "$tmp" in
      *"/nexus-local-ci."*)
        rm -rf "$tmp"
        ;;
    esac
  fi
  if [[ "$post" -eq 1 && "$posted_pending" -eq 1 && "$final_attempted" -eq 0 && -n "$repo" && -n "$sha" ]]; then
    gh api --method POST "repos/${repo}/statuses/${sha}" \
      -f "state=failure" \
      -f "context=local-ci/make-check" \
      -f "description=make check failed" >/dev/null 2>&1 || true
  fi
  if [[ -n "$log" && -f "$log" ]]; then
    printf 'log: %s\n' "$log"
  fi
  exit "$rc"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

repo_name_ok() {
  local name="$1" rest
  case "$name" in
    *[!A-Za-z0-9._/-]*) return 1 ;;
    */*) ;;
    *) return 1 ;;
  esac
  rest="${name#*/}"
  case "$rest" in
    */* | "") return 1 ;;
  esac
  return 0
}

# owner/repo from `git remote get-url origin`, with any userinfo dropped so a
# token embedded in an https URL never reaches the API path or the terminal.
repo_from_origin() {
  local remote path
  remote="$(git remote get-url origin 2>/dev/null)" || return 1
  case "$remote" in
    git@*:*)
      path="${remote#*:}"
      ;;
    *://*)
      path="${remote#*://}"
      path="${path#*@}"
      path="${path#*/}"
      ;;
    *)
      return 1
      ;;
  esac
  path="${path%.git}"
  path="${path%/}"
  repo_name_ok "$path" || return 1
  printf '%s\n' "$path"
}

resolve_repo() {
  local name
  name=""
  if name="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"; then
    if repo_name_ok "$name"; then
      repo="$name"
      return 0
    fi
  fi
  if name="$(repo_from_origin)"; then
    repo="$name"
    return 0
  fi
  return 1
}

post_status() {
  local state="$1"
  local description="$2"
  gh api --method POST "repos/${repo}/statuses/${sha}" \
    -f "state=${state}" \
    -f "context=local-ci/make-check" \
    -f "description=${description}" >/dev/null
}

# Last pytest -q summary, with the trailing " in 12.3s" removed.
# "1157 passed, 3 skipped in 12.3s" -> "1157 passed, 3 skipped"
parse_pytest_summary() {
  local line stripped found piece n word rest
  found=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    piece=""
    case "$line" in
      [0-9]*" in "*s) ;;
      *) continue ;;
    esac
    stripped="${line% in *s}"
    stripped="${stripped%"${stripped##*[![:space:]]}"}"
    stripped="${stripped#"${stripped%%[![:space:]]*}"}"
    rest="$stripped"
    while [[ -n "$rest" ]]; do
      piece="${rest%%,*}"
      piece="${piece#"${piece%%[![:space:]]*}"}"
      piece="${piece%"${piece##*[![:space:]]}"}"
      n="${piece%% *}"
      word="${piece#* }"
      case "$n" in
        "" | *[!0-9]*)
          rest=""
          piece=""
          break
          ;;
      esac
      case "$word" in
        passed | failed | error | errors | skipped | xfailed | xpassed | warning | warnings | rerun | deselected) ;;
        *)
          rest=""
          piece=""
          break
          ;;
      esac
      if [[ "$rest" == *,* ]]; then
        rest="${rest#*,}"
      else
        rest=""
      fi
    done
    if [[ -n "$piece" ]]; then
      found="$stripped"
    fi
  done <"$log"
  printf '%s\n' "$found"
}

summary_is_failure() {
  case "$1" in
    *" failed"* | *" error" | *" error,"* | *" errors" | *" errors,"*) return 0 ;;
  esac
  return 1
}

log_says_lint_failed() {
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      *[Ww]ould\ reformat* | *"would be reformatted"* | "Found "[0-9]*" error."* | "Found "[0-9]*" errors."*)
        return 0
        ;;
    esac
  done <"$log"
  return 1
}

cap_description() {
  local text="$1"
  if [[ "${#text}" -gt 140 ]]; then
    text="${text:0:137}..."
  fi
  printf '%s\n' "$text"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --no-post)
      post=0
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      if [[ -n "$sha_arg" ]]; then
        die "unexpected argument: $1"
      fi
      sha_arg="$1"
      shift
      ;;
  esac
done
if [[ $# -gt 0 ]]; then
  die "unexpected argument: $1"
fi
if [[ -z "$sha_arg" ]]; then
  sha_arg="HEAD"
fi

# Posting checks auth before any git, worktree, or status call. A missing
# login must exit without posting.
if [[ "$post" -eq 1 ]]; then
  if ! gh auth status >/dev/null 2>&1; then
    die "gh is not authenticated; run gh auth login (or use --no-post)"
  fi
fi

if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  die "scripts/local-ci.sh must be run inside a git checkout"
fi
cd "$root"

if ! sha="$(git rev-parse --verify "${sha_arg}^{commit}")"; then
  die "bad SHA: ${sha_arg}"
fi

if [[ "$post" -eq 1 ]]; then
  if ! resolve_repo; then
    die "could not determine owner/repo"
  fi
  if ! post_status "pending" "running make check"; then
    die "failed to post commit status"
  fi
  posted_pending=1
fi

log="$(mktemp "${TMPDIR:-/tmp}/nexus-local-ci-log.XXXXXX")"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/nexus-local-ci.XXXXXX")"
wt="${tmp}/wt"
git worktree add --detach "$wt" "$sha"

if command -v uv >/dev/null 2>&1; then
  uv venv "$wt/.venv"
else
  python3 -m venv "$wt/.venv"
fi
py="${wt}/.venv/bin/python"
if [[ ! -x "$py" ]]; then
  die "venv has no python at ${py}"
fi

dev_rc=0
make_rc=0
set +e
(cd "$wt" && make dev-install "PYTHON=${py}") 2>&1 | tee -a "$log"
dev_rc=${PIPESTATUS[0]}
set -e

state="success"
description="make check passed"
if [[ "$dev_rc" -ne 0 ]]; then
  make_rc="$dev_rc"
  state="failure"
  description="dev-install failed"
else
  set +e
  (cd "$wt" && make check "PYTHON=${py}") 2>&1 | tee -a "$log"
  make_rc=${PIPESTATUS[0]}
  set -e
  summary="$(parse_pytest_summary)"
  if [[ "$make_rc" -eq 0 ]]; then
    state="success"
    if [[ -n "$summary" ]]; then
      description="$summary"
    else
      description="make check passed"
    fi
  else
    state="failure"
    if [[ -n "$summary" ]] && summary_is_failure "$summary"; then
      description="$summary"
    elif log_says_lint_failed; then
      description="lint failed"
    else
      description="make check failed"
    fi
  fi
fi
description="$(cap_description "$description")"

printf 'local-ci: %s: %s\n' "$state" "$description"
if [[ "$post" -eq 1 ]]; then
  final_attempted=1
  if ! post_status "$state" "$description"; then
    printf 'failed to post commit status\n' >&2
    exit 1
  fi
fi
exit "$make_rc"
