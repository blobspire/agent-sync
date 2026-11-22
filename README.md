# agent-sync

[![test](https://github.com/blobspire/agent-sync/actions/workflows/test.yml/badge.svg)](https://github.com/blobspire/agent-sync/actions/workflows/test.yml)

Keep one [Claude Code](https://claude.com/claude-code) setup on all your computers: skills, slash
commands, subagents, `settings.json`, global `CLAUDE.md` instructions, and notes your agents keep. When
an agent on one machine improves a skill, every other machine has the improvement within a few minutes.
Setting up a new computer means giving an agent one README.

It works through a **private git repo you own** (your "agent-config"). agent-sync is the small tool that
installs that repo on each machine and keeps every copy in sync. It talks only to your git remote.

## How it works

```
                 private git repo (GitHub, etc.)
                 ┌───────────────────────────────┐
                 │ skills/ commands/ agents/     │
                 │ settings/base.json            │
                 │ hosts/<host>.json, <host>.md  │
                 │ CLAUDE.md   notes/            │
                 └───────────────────────────────┘
                   ▲  │        ▲  │        ▲  │
   commit, rebase, │  │ pull   │  │        │  │      every 5 minutes, and whenever
            push   │  ▼        │  ▼        │  ▼      an agent runs `agent-sync`
               laptop       desktop      lab box
```

On each machine:

- **Skills, commands and agents** are symlinks from `~/.claude/<kind>/<name>` into the repo checkout, so
  an agent editing `~/.claude/skills/x/SKILL.md` is editing the repo. A new skill created in
  `~/.claude/skills/` is adopted into the repo automatically (after it has been left alone for two minutes,
  so a half-written skill isn't shipped).
- **settings.json** is generated: `settings/base.json` plus this host's overlay in `hosts/<host>.json`, with
  `{{HOME}}`, `{{CONFIG}}` (the repo checkout) and `{{AGENT_SYNC}}` filled in for this machine. When Claude
  Code changes settings itself (`/model`, `/config`, enabling a plugin), the change is written back to this
  host's overlay, never to the shared base, so it sticks on this machine only. Values that look like
  secrets stay in a local-only file and are never committed.
- **CLAUDE.md** stays a regular file. agent-sync manages one marked block in it that imports the repo's
  shared `CLAUDE.md` and this host's `hosts/<host>.md`, and leaves other tools' blocks alone.
- **Notes**: the shared `CLAUDE.md` loads `notes/INDEX.md`, one line per note, and tells agents to keep
  facts that hold on any machine there instead of in a single machine's memory.

A sync is: adopt new local entries and capture settings changes → secret-scan → commit → fetch and
rebase onto the remote → push → re-link and regenerate. It costs no model tokens: it's a script, not an
agent. Running it every 5 minutes is a small git fetch.

## Install

Needs Python 3.9+ and git. No other dependencies.

```bash
git clone https://github.com/blobspire/agent-sync ~/.local/share/agent-sync
~/.local/share/agent-sync/install.sh          # links ~/.local/bin/agent-sync
```

**First machine** (creates your config repo from this machine's current setup):

```bash
gh repo create agent-config --private          # or any empty private git repo
agent-sync new --host laptop --remote https://github.com/<you>/agent-config
agent-sync timer install
```

**Every other machine:**

```bash
agent-sync init https://github.com/<you>/agent-config --host desktop --adopt --dry-run   # preview
agent-sync init https://github.com/<you>/agent-config --host desktop --adopt
agent-sync doctor                               # tools, plugins, hook scripts this machine still lacks
agent-sync timer install
```

`--adopt` makes the first install change nothing about how the machine behaves. Its current settings
become its overlay, its CLAUDE.md text moves into `hosts/<host>.md`, skills that exist only here get
shared, and local copies that differ from the repo's are backed up before being replaced (`--prefer local`
keeps the local version instead). [SETUP.md](SETUP.md) walks an agent through all of this.

## Commands

| Command | What it does |
|---|---|
| `agent-sync` | Sync now (what the timer runs, and what agents run after editing); `--allow-deletes`, `--allow-large`, `--accept-rewrite` release a held sync |
| `agent-sync status` | Last sync, local/remote state, timer, open problems |
| `agent-sync doctor` | Check links, settings, the remote, referenced commands, plugins, `requires` |
| `agent-sync apply [--dry-run]` | Update `~/.claude` from the repo without touching git |
| `agent-sync settings show [--host H]` | This machine's generated settings, or another host's |
| `agent-sync settings factor` | Move settings identical on every host into `base.json` (no host changes) |
| `agent-sync timer install\|remove\|status` | launchd agent (macOS) or systemd user timer (Linux), every 5 min |
| `agent-sync new` / `init` | Set up the first machine / a further machine |
| `agent-sync uninstall` | Turn it off here, replacing links with real copies |

## The config repo

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Instructions for every machine |
| `skills/`, `commands/`, `agents/` | Linked into `~/.claude/` |
| `hooks/`, `bin/` | Scripts settings refer to as `{{CONFIG}}/hooks/...` |
| `settings/base.json` | Settings for every machine |
| `hosts/<host>.json` | `settings` overlay (`null` deletes a key), `exclude: {"skills": [...]}`, `requires: [{"command": "...", "hint": "..."}]` |
| `hosts/<host>.md` | Instructions for one machine |
| `notes/` | Cross-machine notes; `INDEX.md` is loaded every session |
| `agent-sync.json` | `only: {"skills/x": ["laptop"]}`, `ignore`, `notify`, `notify_after_hours`, `max_file_mb` |

## Safety

- **Conflicts**: if two machines change the same file between syncs, the rebase is aborted, local commits
  are kept, and syncing pauses on that machine. A SessionStart hook (added automatically, silent when all
  is well) tells the next agent there exactly how to resolve it. If a problem lasts longer than
  `notify_after_hours`, the `notify` shell command runs with `AGENT_SYNC_HOST`, `AGENT_SYNC_PROBLEM` and
  `AGENT_SYNC_HINT` set.
- **Secrets**: every commit's added lines are scanned for common token shapes and `*_KEY=`-style
  assignments. A hit holds the commit and is reported (file and line, never the value). Add
  `agent-sync: allow` to a line that is a false positive.
- **Accidents**: a sync that deletes more than 20 files, more than half the repo, or more than half of the
  skills, commands, agents or notes (at least 2) is held until you run `agent-sync --allow-deletes`. Files
  over `max_file_mb` (5 MB) are held the same way, and so is any JSON file in the repo that doesn't parse
  (a broken `settings/base.json` would otherwise strip shared settings everywhere). If a broken file
  arrives from elsewhere, machines keep their current settings and only fetch until a fix lands.
- **Cloned skills**: a folder with its own `.git` (a skill installed with `git clone`) isn't adopted,
  because git would store it as an empty submodule. Delete its `.git` to share it, or list it in `ignore`.
- **History rewrites**: if the remote's history is rewritten (a force-push), each machine that still has
  the removed commits pauses with a problem instead of guessing. Run `agent-sync --accept-rewrite` there
  if the rewrite was deliberate (say, purging a leaked secret: the machines then drop those commits too),
  or `git rebase origin/main` in the repo if it was a mistake (the machine's commits are replayed on top).
- **Errors**: any failed sync (a stale `index.lock`, a missing git identity, an unreachable remote on a
  new machine) is recorded and shown by the SessionStart hook, `status` and `doctor`.
- **Backups**: anything agent-sync replaces on a machine is moved to
  `~/.local/state/agent-sync/backups/<time>/` first. Everything in the repo has git history.
- Never touched: `~/.claude.json` (logins), sessions and transcripts, the plugin cache, claude.ai's
  `skills/synced/`, `settings.local.json`, and symlinks that belong to other tools.

See [SECURITY.md](SECURITY.md) for the threat model.

## Limits

- Claude Code only, for now.
- Auto memory stays per machine (Claude Code has no setting to move it). Notes are the shared alternative.
- A settings value of JSON `null` can't be expressed in an overlay (`null` means delete).
- Lists are replaced whole, not merged: if a machine edits a list that comes from `base.json` (say
  `permissions.allow`), its overlay holds the whole list, and later changes to that list in the base no
  longer reach it. Change shared lists in `base.json`, not through one machine.
- Running sessions pick up skill and most settings changes immediately. `CLAUDE.md` changes apply to
  new sessions.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The end-to-end tests simulate several machines with temporary home directories and a bare git repo.

## License

MIT
