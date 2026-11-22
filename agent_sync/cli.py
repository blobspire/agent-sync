"""Command line: `agent-sync` with no command syncs; the rest set up, inspect and maintain."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from . import VERSION, claudemd, links, report, settings, syncer, timer
from .context import AgentSyncError, Context, Paths, default_command, validate_host
from .gitops import Git
from .state import Backups, State, log

TEMPLATE = Path(__file__).resolve().parent.parent / "template"


def _git_identity(args: argparse.Namespace) -> List[str]:
    def get(key: str) -> str:
        r = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True)
        return r.stdout.strip()

    return [args.git_name or get("user.name"), args.git_email or get("user.email")]


def _context(paths: Paths, repo: Path, args: argparse.Namespace) -> Context:
    name, email = _git_identity(args)
    ctx = Context(paths=paths, repo=repo, host=validate_host(args.host), git_name=name, git_email=email,
                  command=default_command())
    ctx.reload()
    return ctx


def _print(lines: List[str]) -> None:
    for line in lines:
        print(line)


def _refuse_if_set_up(paths: Paths, force: bool) -> None:
    if paths.config_file.exists() and not force:
        raise AgentSyncError(
            f"agent-sync is already set up here ({paths.tilde(paths.config_file)}). "
            "Use --force to set it up again."
        )


# ---- commands -----------------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = Context.load(lenient=True)  # a broken repo file must not stop sync from fetching the fix
    return syncer.sync(ctx, quiet=args.quiet, allow_deletes=args.allow_deletes, allow_large=args.allow_large)


def _preflight(args: argparse.Namespace) -> None:
    """Checks that need nothing on disk, done before new/init create or clone anything."""
    validate_host(args.host)
    name, email = _git_identity(args)
    if not name or not email:
        raise AgentSyncError(
            "agent-sync needs a name and email for the commits it makes. Pass --git-name and --git-email, "
            "or set user.name and user.email in your global git config."
        )


def cmd_new(args: argparse.Namespace) -> int:
    paths = Paths.from_env()
    _refuse_if_set_up(paths, args.force)
    _preflight(args)
    repo = Path(args.dir).expanduser().absolute() if args.dir else paths.default_repo
    if repo.exists() and any(repo.iterdir()):
        raise AgentSyncError(f"{repo} already exists and isn't empty.")
    shutil.copytree(TEMPLATE, repo, dirs_exist_ok=True)
    os.chmod(repo, 0o700)
    r = subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], capture_output=True, text=True)
    if r.returncode != 0:  # git older than 2.28
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    ctx = _context(paths, repo, args)
    if args.remote:
        Git(repo).run("remote", "add", "origin", args.remote)
    ctx.save_config()
    st = State(paths.state_dir)
    _print(syncer.apply(ctx, st, Backups(paths.state_dir, paths.claude), adopt=True, prefer="local"))
    st.save()
    print(f"created config repo {paths.tilde(repo)} from this machine (host {ctx.host})")
    return syncer.sync(ctx)


def cmd_init(args: argparse.Namespace) -> int:
    paths = Paths.from_env()
    if not args.dry_run:
        _refuse_if_set_up(paths, args.force)
    _preflight(args)
    repo = Path(args.dir).expanduser().absolute() if args.dir else paths.default_repo
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", args.url, str(repo)], capture_output=True, text=True,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if r.returncode != 0:
            raise AgentSyncError(f"git clone {args.url} failed: {r.stderr.strip()}")
        os.chmod(repo, 0o700)
    elif not (repo / ".git").exists():
        raise AgentSyncError(f"{repo} exists but isn't a git checkout.")
    ctx = _context(paths, repo, args)
    st = State(paths.state_dir)
    backups = Backups(paths.state_dir, paths.claude)
    if args.dry_run:
        print(f"dry run for host {ctx.host} (nothing in {paths.tilde(paths.claude)} changes):")
        _print(syncer.apply(ctx, st, backups, adopt=args.adopt, prefer=args.prefer, dry_run=True) or ["nothing to do"])
        return 0
    ctx.save_config()
    _print(syncer.apply(ctx, st, backups, adopt=args.adopt, prefer=args.prefer))
    st.save()
    return syncer.sync(ctx)


def cmd_apply(args: argparse.Namespace) -> int:
    ctx = Context.load()
    st = State(ctx.paths.state_dir)
    lines = syncer.apply(ctx, st, Backups(ctx.paths.state_dir, ctx.paths.claude), dry_run=args.dry_run)
    if not args.dry_run:
        st.save()
    _print(lines or ["nothing to do"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(report.status(Context.load()))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok, text = report.doctor(Context.load())
    print(text)
    return 0 if ok else 1


def cmd_timer(args: argparse.Namespace) -> int:
    if args.action == "install":
        print(timer.install(Context.load()))
    elif args.action == "remove":
        print(timer.remove())
    else:
        print(timer.status()[1])
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    try:
        text = report.session_hook(Paths.from_env())
    except Exception as exc:  # a hook must never break session start
        text = f"agent-sync's session check failed: {exc}"
    if text:
        print(text)
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    ctx = Context.load()
    if args.action == "show":
        print(report.show_settings(ctx, args.host or ""))
    else:
        hosts = sorted(p.stem for p in (ctx.repo / "hosts").glob("*.json"))
        base = settings.factor(ctx, hosts)
        print(f"base.json now holds {len(base)} top-level key(s) shared by {', '.join(hosts)}; "
              "each host's settings are unchanged. Run `agent-sync` to commit.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    ctx = Context.load()
    print(timer.remove())
    copied = links.materialize(ctx)
    if copied:
        print("replaced links with copies: " + ", ".join(copied))
    if claudemd.strip(ctx):
        print("CLAUDE.md: replaced the agent-sync block with the text it imported")
    try:
        current = settings.read_current(ctx)
        if current is not None:
            from . import jsonutil
            jsonutil.write_atomic(settings.settings_path(ctx), jsonutil.dumps(settings.strip_tool_hook(current)))
    except settings.Unreadable:
        print("settings.json isn't valid JSON; remove agent-sync's SessionStart hook from it by hand")
    ctx.paths.config_file.unlink(missing_ok=True)
    print(f"agent-sync is off on this machine. The config repo stays at {ctx.paths.tilde(ctx.repo)}.")
    return 0


# ---- parser -------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-sync",
        description="Keep one Claude Code setup (skills, commands, agents, settings, instructions, notes) "
        "in sync across your computers through a private git repo. With no command: sync now.",
    )
    p.add_argument("--version", action="version", version=f"agent-sync {VERSION}")
    p.add_argument("--quiet", action="store_true", help="print nothing (for the timer); problems go to the log")
    p.add_argument("--allow-deletes", action="store_true", help="sync a large deletion that was held")
    p.add_argument("--allow-large", action="store_true", help="sync large files that were held")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def identity(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--host", required=True, help="a short name for this machine, e.g. laptop")
        sp.add_argument("--dir", help="where to keep the config repo (default ~/.local/share/agent-config)")
        sp.add_argument("--git-name", help="commit author name (default: git config user.name)")
        sp.add_argument("--git-email", help="commit author email (default: git config user.email)")
        sp.add_argument("--force", action="store_true", help="set up again even if already set up")

    sp = sub.add_parser("new", help="create a config repo from this machine's current setup")
    identity(sp)
    sp.add_argument("--remote", help="git URL of an empty private repo to push to")
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("init", help="set up this machine from an existing config repo")
    sp.add_argument("url", help="git URL of your config repo")
    identity(sp)
    sp.add_argument("--adopt", action="store_true",
                    help="first install: keep this machine's settings and CLAUDE.md text as its host overlay, "
                         "adopt its skills, and back up local copies that differ")
    sp.add_argument("--prefer", choices=("repo", "local"), default="repo",
                    help="with --adopt, which version wins when a skill exists in both (default repo)")
    sp.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("apply", help="update ~/.claude from the repo without touching git")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_apply)

    sub.add_parser("status", help="what was synced when, and anything stuck").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="check this machine's setup").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("timer", help="run sync every 5 minutes (launchd or systemd)")
    sp.add_argument("action", choices=("install", "remove", "status"))
    sp.set_defaults(func=cmd_timer)

    sp = sub.add_parser("hook", help="Claude Code hook entry points")
    sp.add_argument("event", choices=("session-start",))
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("settings", help="show this or another host's settings, or factor shared ones into base")
    sp.add_argument("action", choices=("show", "factor"))
    sp.add_argument("--host", help="with show: another host's effective settings (placeholder form)")
    sp.set_defaults(func=cmd_settings)

    sub.add_parser("uninstall", help="turn agent-sync off here, leaving real copies").set_defaults(func=cmd_uninstall)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    func = getattr(args, "func", cmd_sync)
    try:
        return func(args)
    except AgentSyncError as exc:
        print(f"agent-sync: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        detail = traceback.format_exc()
        try:
            log(Paths.from_env().state_dir, "crash:\n" + detail)
        except Exception:
            pass
        if not getattr(args, "quiet", False):
            print(detail, file=sys.stderr)
        return 2
