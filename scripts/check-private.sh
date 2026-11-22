#!/usr/bin/env bash
# check-private.sh: refuse to publish a public repo that mentions any term from a private denylist.
#
#   scripts/check-private.sh <denylist-file> [--history]
#
# The denylist is a plain text file, one term per line (case-insensitive, fixed strings; blank lines and
# lines starting with # are skipped). Keep it somewhere private, for example in your config repo, never in
# the public repo itself. Checks every tracked file, and with --history every commit message and diff.
#
# As a pre-push hook in your clone:
#   git config agent-sync.denylist /path/to/denylist.txt
#   printf '#!/bin/sh\nexec scripts/check-private.sh "$(git config agent-sync.denylist)" --history\n' > .git/hooks/pre-push
#   chmod +x .git/hooks/pre-push
set -euo pipefail

list=${1:-}
if [ -z "$list" ] || [ ! -f "$list" ]; then
  echo "check-private: denylist file not found: '${list}'" >&2
  exit 2
fi
terms=$(mktemp)
trap 'rm -f "$terms"' EXIT
grep -v '^[[:space:]]*#' "$list" | grep -v '^[[:space:]]*$' > "$terms" || true
if [ ! -s "$terms" ]; then
  echo "check-private: denylist is empty" >&2
  exit 2
fi

found=0
if git grep -n -I -i -F -f "$terms" -- . ':!*denylist*' >/dev/null 2>&1; then
  echo "check-private: tracked files mention private terms:" >&2
  git grep -n -I -i -o -F -f "$terms" -- . ':!*denylist*' >&2 || true
  found=1
fi
if [ "${2:-}" = --history ]; then
  hits=$(git log --all -p --format='commit %h%n%B' | grep -n -i -o -F -f "$terms" | sort -u || true)
  if [ -n "$hits" ]; then
    echo "check-private: history mentions private terms (line: term):" >&2
    echo "$hits" | head -40 >&2
    found=1
  fi
fi
if [ "$found" = 0 ]; then
  echo "check-private: clean ($(wc -l < "$terms" | tr -d ' ') terms checked)"
fi
exit "$found"
