# Shared setup (synced by agent-sync)

This machine's skills, commands, agents, settings and these instructions come from one git repo that agent-sync keeps in sync across all of the user's computers. The repo is at `~/.claude/agent-config`.

- Skills in `~/.claude/skills/` that link into the repo are shared: improving one improves it on every machine. A new skill created in `~/.claude/skills/` is adopted and shared automatically.
- After you change a skill, command, agent, shared setting or note, run `agent-sync` so it goes out now instead of within five minutes.
- Settings changed here (`/model`, `/config`, editing `settings.json`) stay on this machine. To change a setting everywhere, edit `settings/base.json` in the repo.
- Instructions for every machine go in the repo's `CLAUDE.md`; for one machine, in its `hosts/<host>.md`. Don't add text to `~/.claude/CLAUDE.md` outside the managed blocks.
- **Notes:** a fact that holds on any machine (how to reach a computer, shared infrastructure, a tool quirk) goes in the repo's `notes/`, one file per fact, plus a one-line entry in `notes/INDEX.md`. Facts about a single project stay in that project's memory. A standing preference the user has stated goes in this file as one line with its reason, not in a note.
- Before adding a note, look for one that already covers it and edit that. Keep something only if it is a fact a model can't find on its own, a preference the user stated as standing, a constraint that prevents real harm, or a tool behaviour verified on the current version (say which). A correction replaces the old text instead of being appended. A passing remark is not a standing rule unless the user says it is. Delete notes that turn out wrong, and remove a project's copy of anything moved into `notes/`.

@notes/INDEX.md
