#!/usr/bin/env bash
# Fails when a commit in the range has an author or committer other than the
# project noreply identity, or when a personal mailbox address is in the tree.
#
# Usage: scripts/check_commit_identity.sh [RANGE]
#   RANGE defaults to $CHECK_RANGE, else origin/main..HEAD, else HEAD alone.
#   CI passes the pull request range: <base sha>..<head sha>.
set -euo pipefail

ALLOWED="314062777+olympusbuildz@users.noreply.github.com"
ALLOWED_NAME="olympusbuildz"
PERSONAL_DOMAINS='out[l]ook|hot[m]ail|live|msn|g[m]ail|goo[g]lemail|y[a]hoo|y[m]ail|i[c]loud|me|mac|aol|proton|pm|gmx|zoho|fastmail'

cd "$(git rev-parse --show-toplevel)"

range="${1:-${CHECK_RANGE:-}}"
if [[ -z "${range}" ]]; then
  if git rev-parse --verify --quiet origin/main >/dev/null; then
    range="origin/main..HEAD"
  else
    range="HEAD^!"
    echo "check_commit_identity: no origin/main; checking HEAD only" >&2
  fi
fi

failed=0

while IFS=$'\t' read -r sha author_name author_email committer_name committer_email; do
  [[ -z "${sha}" ]] && continue
  if [[ "${author_email}" != "${ALLOWED}" ]]; then
    echo "commit ${sha:0:12}: author is ${author_name} <${author_email}>" >&2
    failed=1
  fi
  if [[ "${committer_email}" != "${ALLOWED}" ]]; then
    echo "commit ${sha:0:12}: committer is ${committer_name} <${committer_email}>" >&2
    failed=1
  fi
done < <(git log --format='%H%x09%an%x09%ae%x09%cn%x09%ce' "${range}")

hits="$(git grep -I -n -i --untracked -E "[A-Za-z0-9._%+-]+@(${PERSONAL_DOMAINS})\.[A-Za-z]{2,}" || true)"
if [[ -n "${hits}" ]]; then
  echo "personal mailbox addresses in the tree:" >&2
  echo "${hits}" | cut -d: -f1,2 >&2
  failed=1
fi

if (( failed )); then
  cat >&2 <<EOF

Why this fails: every commit's author and committer must be
${ALLOWED_NAME} <${ALLOWED}>, and no personal mailbox address may be committed.
Fix: git config user.name ${ALLOWED_NAME}; git config user.email ${ALLOWED}
then rewrite the offending commits (git rebase -r origin/main --exec 'git commit --amend --reset-author --no-edit'),
and delete the address from the tree.
EOF
  exit 1
fi
echo "commit identity OK (${range})"
