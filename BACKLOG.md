# Backlog

Known limitations and low-severity issues found in review, not yet fixed. None of them loses data on the
normal path; each is either rare, cosmetic, or only keeps something local that could have synced.

## Sync and git
- Rewrite detection relies on the remote-tracking reflog: with `core.logAllRefUpdates=false`, or a reflog
  expired by `gc` on a machine offline for a month, a purge can be re-pushed. A recorded "last seen remote
  tip" would remove the dependency.
- `--accept-rewrite`, and the hint's `git rebase origin/main`, fail while the working tree is dirty
  (common during recovery). Use `--autostash`, or commit first.
- If the repo config breaks while this machine has an unpushed commit, recovery can't fast-forward and stays
  paused until the file is fixed locally; a stale "push" problem can stay open alongside.
- A "rewrite" problem can stay open while a later run is held for another reason.
- A push declined by a server rule is misread as non-fast-forward ("the remote kept moving"); the detached
  HEAD case gets a credentials hint.
- The per-kind deletion hold trips on legitimate housekeeping in small repos (renaming 2 of 3 skills,
  merging 2 of 3 notes). `--allow-deletes` releases it.
- `apply`, `new`, `init` and `settings factor` don't take the sync lock.
- A stale `index.lock` is removed after 10 minutes, even if a person's `git commit` editor is still open.
- The "remote" problem quotes the remote URL, which could include an embedded token.
- `notify` doesn't fire while the repo's own config is broken (the manifest isn't loaded).
- Untested seams: recovery never pushing; the fetch after a rejected push; stale-lock removal in recovery.

## Settings
- Lists are replaced whole in overlays (documented in README Limits); a set-style list diff would let a
  machine add to a shared list without pinning it.
- A stale full-copy rewrite of settings.json (an editor holding an old copy) pins old base values into the
  host overlay.
- Secret env-name coverage: generic `*_KEY` names (ENCRYPTION_KEY, SIGNING_KEY, MASTER_KEY,
  SUPABASE_SERVICE_ROLE_KEY), `*_PWD`/`*_PW`, `*_PAT`, AUTHORIZATION, BEARER, and ANTHROPIC_CUSTOM_HEADERS
  carrying `Authorization: Bearer ...` still sync when the value has no recognizable shape. False positives
  (AUTH_URL, SSH_AUTH_SOCK, *_CREDENTIALS_FILE, PASSWORD_MIN_LENGTH) only stay local.
- Secret scan misses: binary files, added lines starting with `++`, webhook and database URLs, `hf_` tokens.
- A symlinked settings.json or CLAUDE.md (from a dotfiles manager) is replaced by a regular file.
- `settings factor` treats hosts without a host file as having no settings.
- JSON `null` values are dropped on adopt; a literal `{{HOME}}` in a user string doesn't round-trip.

## Files and CLAUDE.md
- Other tools' CLAUDE.md blocks with CRLF line endings, trailing spaces after `-->`, or names with spaces
  count as unmanaged text.
- `rebase_imports` also rewrites `@`-tokens that aren't imports (`@types/node`, `@scope/pkg`, decorators in
  code fences) when adoption moves text or uninstall inlines it.
- Strict marker parsing treats a document that quotes `<!-- BEGIN agent-sync` in prose as damaged.
- Uninstall stops with a traceback when the markers are damaged, and a copy failure mid-uninstall leaves a
  partial copy.
- After the checkout moves, old links count as "foreign" until removed by hand; install.sh won't relink.
- A folder with its own `.git` that is never adopted is walked every run.

## Setup and tooling
- `_preflight` reads only `git config --global`, missing identities from `[include]` files or
  `GIT_AUTHOR_*` env; `init --dry-run` also requires an identity.
- `init` with an existing checkout doesn't compare its remote to the URL given; `init --dry-run` clones.
- systemd unit text doesn't escape `%` or `$` in paths; `default_command()` can record a relative path.
- `scripts/check-private.sh` doesn't check commit author fields, binary files, or file names without
  `--history`, skips paths containing "denylist", and reports "clean" outside a git repo.
