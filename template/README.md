# agent-config

This private repo holds one person's Claude Code setup: skills, commands, agents, settings, global
instructions and notes. [agent-sync](https://github.com/blobspire/agent-sync) installs it on each of their
computers and keeps every copy in sync.

## Set up a new computer (instructions for an agent)

Work through these in order. Ask the user before installing software or using `sudo`.

1. **Install agent-sync.**
   ```bash
   git clone https://github.com/blobspire/agent-sync ~/.local/share/agent-sync
   ~/.local/share/agent-sync/install.sh
   ```
2. **Make sure git can reach this repo.** With GitHub: `gh auth login`, then `gh auth setup-git`. Logins
   need the user's hands; ask them to run those.
3. **Pick a short name for this computer** with the user (for example `laptop` or `desktop`). If a file in
   `hosts/` already matches this computer, use that name.
4. **Preview, then install.**
   ```bash
   agent-sync init <this repo's git URL> --host <name> --adopt --dry-run
   agent-sync init <this repo's git URL> --host <name> --adopt
   ```
   `--adopt` keeps this computer's current settings as its own overlay (nothing changes behaviour on the
   first install), shares any skills that exist only here, and backs up local copies that differ.
5. **Fill the gaps.** `agent-sync doctor` lists commands, plugins and tools the settings refer to that this
   computer doesn't have yet. Install them (see `requires` in `hosts/*.json` for hints) or remove the
   reference from this host's overlay.
6. **Turn on syncing every 5 minutes:** `agent-sync timer install`.

## Layout

| Path | What it is |
|---|---|
| `CLAUDE.md` | Instructions for every machine (imported by each machine's `~/.claude/CLAUDE.md`) |
| `skills/`, `commands/`, `agents/` | Linked into `~/.claude/` on every machine |
| `hooks/`, `bin/` | Scripts that settings refer to as `{{CONFIG}}/hooks/...` |
| `settings/base.json` | Settings for every machine |
| `hosts/<host>.json` | One machine's settings overlay, plus `exclude` and `requires` |
| `hosts/<host>.md` | Instructions for one machine |
| `notes/` | Facts that hold on any machine; `notes/INDEX.md` is loaded into every session |
| `agent-sync.json` | Repo options: host-only entries (`only`), `ignore`, `notify` |
