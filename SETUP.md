# Setting up agent-sync (instructions for an agent)

You're setting up agent-sync for a user so their Claude Code setup (skills, commands, agents, settings,
global instructions, notes) is shared across their computers through a private git repo. Work through the
steps in order. Run commands yourself where you can. When a step needs the user's hands (a login, a
password, choosing a name), tell them exactly what to run and wait.

Ground rules:

- Ask before installing software, and before anything that needs `sudo`.
- Never put the config repo anywhere public. It holds the user's settings and notes.
- Don't edit `~/.claude.json`, and don't log in or answer trust prompts for the user.
- Use `--dry-run` first and show the user what will change before running for real.

## 1. Install agent-sync

```bash
if [ -d ~/.local/share/agent-sync/.git ]; then
  git -C ~/.local/share/agent-sync pull --ff-only
else
  git clone https://github.com/blobspire/agent-sync ~/.local/share/agent-sync
fi
~/.local/share/agent-sync/install.sh
agent-sync --version
```

## 2. Git access to the config repo

The config repo is private, so git on this machine needs credentials for it. With GitHub, the simplest
is the GitHub CLI: the user runs `gh auth login`, then `gh auth setup-git`. Check with
`git ls-remote <repo-url>`.

## 3a. First machine: create the config repo

Ask the user for a short name for this computer (e.g. `laptop`) and create an **empty private** repo
(`gh repo create agent-config --private`). Then:

```bash
agent-sync new --host laptop --remote <repo-url>
```

This copies the current skills, commands, agents and settings into the repo, links them back, and pushes.
Read the output with the user: anything listed under "keeping on this machine only (look like secrets)"
stayed out of the repo.

## 3b. Another machine: join the existing config repo

If the repo has a `hosts/<name>.json` for this computer, use that name. Otherwise pick a new one.

```bash
agent-sync init <repo-url> --host desktop --adopt --dry-run
```

Show the user the plan:

- `replace local copy with link (local copy differs; backed up)`: this machine has its own version of a
  shared skill. Compare them first. If the local one is better, either rerun with `--prefer local`, or merge
  the improvements into the repo version after installing.
- `adopt into repo`: a skill only this machine had will now be shared with every machine.
- `settings.json: would change ...`: with `--adopt`, only agent-sync's own SessionStart hook should appear.

Then run it for real (without `--dry-run`).

## 4. Fill the gaps

```bash
agent-sync doctor
```

`FAIL` lines name hook scripts, status-line commands, plugins, or `requires` entries this machine lacks.
Install them (ask first), or, if a setting doesn't apply on this machine, override it in this host's
overlay (`hosts/<host>.json`, `"settings": {"key": null}` deletes a key) and run `agent-sync`.

## 5. Turn on automatic syncing

```bash
agent-sync timer install
agent-sync status
```

If a permission system blocks installing a background service, give the user the command to run
themselves.

## 6. Confirm

- `ls -l ~/.claude/skills` shows links into `~/.local/share/agent-config`.
- `agent-sync doctor` has no `FAIL` lines.
- Edit nothing else by hand: from now on, change shared things in `~/.claude/agent-config/` (or through
  the links) and run `agent-sync`.
