#!/usr/bin/env bash
# Install agent-sync: link the `agent-sync` command onto your PATH.
# Safe to re-run, for example after `git pull`. `./install.sh --uninstall` removes the link.
set -euo pipefail

REPO=$(cd -P "$(dirname "$0")" && pwd)
BIN=${AGENT_SYNC_BIN_DIR:-$HOME/.local/bin}
ours() { [ -L "$1" ] && [ "$(readlink "$1")" = "$2" ]; }

if [ "${1:-}" = --uninstall ]; then
  if ours "$BIN/agent-sync" "$REPO/agent-sync"; then
    rm "$BIN/agent-sync" && echo "removed $BIN/agent-sync"
  fi
  echo "To turn syncing off on this machine first, run: agent-sync uninstall"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "agent-sync needs python3 (3.9 or newer) and git." >&2
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "agent-sync needs Python 3.9 or newer; python3 is $(python3 --version 2>&1)." >&2
  exit 1
}

mkdir -p "$BIN"
chmod +x "$REPO/agent-sync"
if ours "$BIN/agent-sync" "$REPO/agent-sync"; then
  echo "ok       $BIN/agent-sync"
elif [ -e "$BIN/agent-sync" ] || [ -L "$BIN/agent-sync" ]; then
  echo "SKIPPED  $BIN/agent-sync already exists and isn't this checkout's. Move it aside and run this again."
  exit 1
else
  ln -s "$REPO/agent-sync" "$BIN/agent-sync"
  echo "linked   $BIN/agent-sync -> $REPO/agent-sync"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Note: $BIN isn't on your PATH. Add it in your shell profile." ;;
esac
echo "Next: agent-sync new --host <name> (first machine) or agent-sync init <config-repo-url> --host <name> --adopt"
