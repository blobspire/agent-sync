"""apply (repo -> this machine) and sync (this machine <-> repo <-> remote)."""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional, Tuple

from . import claudemd, jsonutil, links, scan, settings
from .context import AgentSyncError, Context
from .gitops import Git, GitError
from .state import Backups, State, lock, log, now

DELETE_LIMIT = 20  # more deleted files than this in one sync is held for a human
LARGE_MB = 5


# ---- apply --------------------------------------------------------------------------------------


def apply(
    ctx: Context, st: State, backups: Backups, adopt: bool = False, prefer: str = "repo", dry_run: bool = False
) -> List[str]:
    """Bring ~/.claude in line with the repo. Returns report lines; records problems in `st`."""
    lines: List[str] = []
    for problem in links.ensure_repo_link(ctx, dry_run):
        lines.append("! " + problem)

    # settings.json: capture local edits first so regenerating never throws them away
    try:
        local = None
        if adopt:
            secrets = settings.adopt(ctx, save=not dry_run)
            if dry_run:
                local = secrets
            if secrets:
                lines.append(
                    "settings: keeping on this machine only (look like secrets): "
                    + ", ".join(jsonutil.leaf_paths(secrets))
                )
        else:
            captured = settings.capture(ctx, dry_run)
            if captured:
                lines.append(f"settings: kept this machine's change(s) to {_short(captured)} (hosts/{ctx.host}.json)")
        changed = settings.write(ctx, backups, dry_run, local)
        if changed:
            verb = "would change" if dry_run else "updated"
            lines.append(f"settings.json: {verb} {_short(changed)}")
        st.close_problem("settings")
    except settings.Unreadable as exc:
        st.open_problem(
            "settings",
            f"{ctx.paths.tilde(settings.settings_path(ctx))} isn't valid JSON ({exc}), so agent-sync left it alone.",
            "Fix the JSON; the next sync picks it up.",
        )

    # skills, commands, agents
    actions = links.plan(ctx, adopt_all=adopt, prefer=prefer)
    lines += [("would " if dry_run else "") + a.describe() for a in actions if not a.is_problem]
    problems = [a.describe() for a in actions if a.is_problem] if dry_run else links.apply(ctx, actions, backups)
    if problems:
        st.open_problem("links", " ".join(problems))
        lines += ["! " + p for p in problems]
    else:
        st.close_problem("links")

    # CLAUDE.md
    lines += [("would: " if dry_run else "") + n for n in claudemd.apply(ctx, backups, adopt=adopt, dry_run=dry_run)]
    doc_path = claudemd.path(ctx)
    extra = claudemd.outside_text(doc_path.read_text(encoding="utf-8")) if doc_path.exists() else ""
    if extra and not (adopt and dry_run):
        st.open_problem(
            "claude-md",
            f"{ctx.paths.tilde(doc_path)} has {len(extra.splitlines())} line(s) outside any managed block, "
            "so they don't reach your other machines.",
            f"Move them into {ctx.paths.tilde(ctx.paths.repo_link / 'CLAUDE.md')} (every machine) or "
            f"hosts/{ctx.host}.md there (this machine only), then delete them from {ctx.paths.tilde(doc_path)}.",
        )
    else:
        st.close_problem("claude-md")

    if backups.used and not dry_run:
        lines.append(f"backups: {ctx.paths.tilde(backups.root)}")
    return lines


def _short(paths: List[str], n: int = 6) -> str:
    return ", ".join(paths[:n]) + (f" +{len(paths) - n} more" if len(paths) > n else "")


# ---- sync ---------------------------------------------------------------------------------------


GROUPED = ("skills", "commands", "agents", "notes", "hosts", "settings", "hooks", "bin")


def commit_message(host: str, pairs: List[Tuple[str, str]], first: bool) -> str:
    if first:
        return f"{host}: initial setup"
    groups = []
    for _, path in pairs:
        parts = path.split("/")
        groups.append("/".join(parts[:2]) if parts[0] in GROUPED and len(parts) > 1 else path)
    groups = jsonutil.unique(groups)
    summary = ", ".join(groups[:4]) + (f" +{len(groups) - 4} more" if len(groups) > 4 else "")
    return f"{host}: {summary}"


def outgoing_issues(git: Git, pairs: List[Tuple[str, str]], allow_deletes: bool, allow_large: bool, max_mb: float) -> List[str]:
    issues = []
    findings = scan.scan_added_lines(git.staged_patch())
    if findings:
        issues.append("possible secrets: " + "; ".join(findings[:8]) + (" ..." if len(findings) > 8 else ""))
    deleted = [p for s, p in pairs if s == "D"]
    total = git.tracked_count() + len(deleted)
    if not allow_deletes and len(deleted) >= 3 and (len(deleted) > DELETE_LIMIT or len(deleted) * 2 > total):
        issues.append(f"{len(deleted)} of {total} files deleted at once (e.g. {', '.join(deleted[:3])})")
    big = [f"{p} ({size / 1048576:.1f} MB)" for p, size in git.staged_sizes() if size > max_mb * 1048576]
    if big and not allow_large:
        issues.append("large files: " + ", ".join(big[:5]))
    return issues


def _reload(ctx: Context, st: State) -> bool:
    try:
        ctx.reload()
        st.close_problem("repo")
        return True
    except AgentSyncError as exc:
        st.open_problem("repo", str(exc))
        return False


def sync(ctx: Context, quiet: bool = False, allow_deletes: bool = False, allow_large: bool = False) -> int:
    st = State(ctx.paths.state_dir)
    with lock(ctx.paths.state_dir, wait=not quiet) as got:
        if not got:
            if not quiet:
                print("agent-sync: another sync is running; try again in a moment.")
            return 0 if quiet else 1
        lines: List[str] = []
        try:
            _sync(ctx, st, lines, allow_deletes, allow_large)
        finally:
            _notify(ctx, st)
            st.data["last_run"] = now()
            st.save()
            if lines:
                log(ctx.paths.state_dir, "\n".join(lines))
        if not quiet:
            for line in lines:
                print(line)
            for p in st.problems.values():
                print(f"! {p['message']}" + (f"\n  {p['hint']}" if p.get("hint") else ""))
            if not lines and not st.problems:
                print("up to date")
        return 0


def _sync(ctx: Context, st: State, lines: List[str], allow_deletes: bool, allow_large: bool) -> None:
    git = Git(ctx.repo, ctx.git_name, ctx.git_email)
    backups = Backups(ctx.paths.state_dir, ctx.paths.claude)

    busy = git.busy()
    if busy:
        st.open_problem(
            "conflict",
            f"A git {busy} is in progress in {ctx.paths.tilde(ctx.paths.repo_link)}, so syncing is paused.",
            f"Finish it (`git -C {ctx.paths.tilde(ctx.paths.repo_link)} status` shows what's left), then run `agent-sync`.",
        )
        lines += apply(ctx, st, backups)
        return

    # 1. This machine's changes into the repo: adopt new skills, capture settings edits, commit.
    lines += apply(ctx, st, backups)
    pairs = git.stage_all()
    if pairs:
        issues = outgoing_issues(git, pairs, allow_deletes, allow_large, float(ctx.manifest.get("max_file_mb", LARGE_MB)))
        if issues:
            git.unstage()
            st.open_problem(
                "held",
                "Local changes were NOT synced: " + " | ".join(issues) + ".",
                "Remove secrets (put them in settings.local.json or an env var), or if the deletions/large files "
                "are intended, run `agent-sync --allow-deletes` / `--allow-large`.",
            )
            return
        first = not git.has_commits()
        git.commit(commit_message(ctx.host, pairs, first))
        lines.append(f"committed {len(pairs)} change(s): {commit_message(ctx.host, pairs, False).split(': ', 1)[1]}")
    st.close_problem("held")

    # 2. Exchange with the remote.
    if not git.has_remote():
        st.data["last_ok"] = now()
        return
    try:
        git.fetch()
    except GitError as exc:
        st.data["last_error"] = str(exc)
        lines.append(f"remote unreachable, will retry: {exc}")
        return
    branch = git.branch()
    _, behind = git.ahead_behind(branch)
    incoming = bool(behind)
    if behind:
        conflicted = git.rebase_onto_remote(branch)
        if conflicted:
            _open_conflict(ctx, st, conflicted)
            return
        lines.append(f"pulled {behind} change(s)")
        if not _reload(ctx, st):
            return
    st.close_problem("conflict")

    ahead, _ = git.ahead_behind(branch)
    if ahead:
        for _attempt in range(3):
            try:
                if git.push(branch):
                    lines.append(f"pushed {ahead} commit(s)")
                    st.close_problem("push")
                    break
            except GitError as exc:
                st.open_problem("push", f"Couldn't push to the remote: {exc}.", "Check git credentials for the remote.")
                return
            git.fetch()
            conflicted = git.rebase_onto_remote(branch)
            if conflicted:
                _open_conflict(ctx, st, conflicted)
                return
            incoming = True
            if not _reload(ctx, st):
                return
            ahead, _ = git.ahead_behind(branch)
        else:
            st.open_problem("push", "The remote kept moving while pushing; will retry next run.")
            return
    st.data["last_ok"] = now()
    st.data.pop("last_error", None)

    # 3. Install whatever came in.
    if incoming:
        lines += apply(ctx, st, backups)


def _open_conflict(ctx: Context, st: State, files: List[str]) -> None:
    repo = ctx.paths.tilde(ctx.paths.repo_link)
    st.open_problem(
        "conflict",
        f"This machine's changes to {', '.join(files[:5])} conflict with changes from another machine, so "
        "syncing is paused (local commits are kept).",
        f"Resolve it: `cd {repo} && git pull --rebase`, merge the conflicted files keeping both sides' intent, "
        "`git add` them, `git rebase --continue`, then run `agent-sync`.",
    )


def _notify(ctx: Context, st: State) -> None:
    command = ctx.manifest.get("notify")
    if not command:
        return
    after = float(ctx.manifest.get("notify_after_hours", 6)) * 3600
    for key, problem in list(st.problems.items()):
        if now() - problem["since"] < after or key in st.data["notified"]:
            continue
        env = dict(
            os.environ,
            AGENT_SYNC_HOST=ctx.host,
            AGENT_SYNC_PROBLEM=problem["message"],
            AGENT_SYNC_HINT=problem.get("hint", ""),
        )
        try:
            result = subprocess.run(command, shell=True, env=env, capture_output=True, text=True, timeout=60)
            ok = result.returncode == 0
            detail = (result.stderr or result.stdout).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, detail = False, str(exc)
        if ok:
            st.data["notified"][key] = now()
        else:
            log(ctx.paths.state_dir, f"notify command failed for {key}: {detail}")
