# Security

## What agent-sync touches

- **Network**: only your config repo's git remote, through your own git credentials (`git fetch`, `git push`).
  No telemetry, no other hosts.
- **Files**: `~/.claude/{skills,commands,agents}/` (symlinks it created), `~/.claude/settings.json`,
  agent-sync's block in `~/.claude/CLAUDE.md`, the link `~/.claude/agent-config`, its config in
  `~/.config/agent-sync/`, and its state in `~/.local/state/agent-sync/`. Never `~/.claude.json`,
  credentials, sessions or transcripts.
- **Programs**: `git`, `launchctl`/`systemctl` for the timer, and the repo's `notify` command, if you set one.

## Threat model

The config repo is the trust root. **Keep it private.** Anyone who can push to it can change the skills,
hook commands and instructions that Claude Code runs on every one of your machines. That's the point of
the tool, but it means repo write access is equivalent to running code as you on each machine. Protect
it like an SSH key: limit collaborators, use credentials scoped to it where your host allows, and review
`git log -p` if something looks off.

Consequences worth knowing:

- A change pushed from any machine (including by an agent there) reaches the others within minutes.
  Every change is a commit, so `git revert` undoes it everywhere.
- Hook commands in `settings/base.json` run on every machine. Treat edits to hooks with the same care as
  edits to a shell profile.
- `notify` in `agent-sync.json` is a shell command run by the sync on every machine.

## Secrets

Secrets don't belong in the config repo, even a private one. agent-sync helps keep them out:

- Before each commit, added lines are scanned for private keys, GitHub/Anthropic/OpenAI/AWS/Slack/Google
  token shapes, and long mixed values assigned to names containing key, secret, token or password. A hit
  holds the commit. The report names the file, line and kind, never the value.
- In `settings.json`, values with those shapes and `env` entries named like `*_TOKEN`, `*_SECRET` or
  `*_API_KEY` are kept in `~/.local/state/agent-sync/local-settings.json` (mode 600) on that machine only.

The scan is a seatbelt, not a guarantee: it can't recognize every secret. Prefer `settings.local.json`,
environment variables or a secret manager.

## Reporting a vulnerability

Open a [GitHub security advisory](https://github.com/blobspire/agent-sync/security/advisories/new) on this
repo, or an issue without exploit details asking for a private contact.
