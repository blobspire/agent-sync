"""status, doctor and the SessionStart hook: reading state and checking the machine."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Iterator, List, Tuple

from . import VERSION, claudemd, jsonutil, links, settings, timer
from .context import AgentSyncError, Context, Paths
from .gitops import Git, GitError
from .state import State, now, stamp

STALE_SECONDS = 24 * 3600


def _age(ts: float) -> str:
    secs = max(0, int(now() - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def session_hook(paths: Paths) -> str:
    """Text for the SessionStart hook: empty unless something needs an agent's attention."""
    try:
        ctx = Context.load(paths)
    except AgentSyncError as exc:
        if not paths.config_file.exists():
            return ""
        return f"agent-sync (shared Claude Code setup) is broken on this machine: {exc}"
    st = State(paths.state_dir)
    items = []
    for problem in st.problems.values():
        items.append(f"- {problem['message']}" + (f" {problem['hint']}" if problem.get("hint") else ""))
    last_ok = st.data.get("last_ok")
    if last_ok and now() - last_ok > STALE_SECONDS:
        items.append(
            f"- It hasn't synced with the remote for {_age(last_ok)}"
            + (f" (last error: {st.data['last_error']})" if st.data.get("last_error") else "")
            + ". Run `agent-sync doctor`."
        )
    if not items:
        return ""
    return (
        f"agent-sync: the shared Claude Code setup on this machine ({ctx.host}; skills, settings and notes in "
        f"{paths.tilde(paths.repo_link)}) needs attention:\n" + "\n".join(items) + "\n"
        "Fix it if it's within reach of your task; otherwise tell the user."
    )


def status(ctx: Context) -> str:
    st = State(ctx.paths.state_dir)
    git = Git(ctx.repo)
    out = [f"agent-sync {VERSION} · host {ctx.host}"]
    remote = git.remote_url() if git.has_remote() else "(no remote)"
    out.append(f"repo      {ctx.paths.tilde(ctx.repo)}  ←→  {remote}")
    last_run, last_ok = st.data.get("last_run"), st.data.get("last_ok")
    out.append(
        "last sync "
        + (f"{stamp(last_run)} ({_age(last_run)} ago)" if last_run else "never")
        + " · reached remote "
        + (f"{_age(last_ok)} ago" if last_ok else "never")
    )
    dirty = git.dirty()
    out.append("local     " + (f"{len(dirty)} change(s) not yet committed" if dirty else "clean"))
    if git.has_remote():
        try:
            ahead, behind = git.ahead_behind(git.branch())
            out.append(f"remote    {ahead} ahead, {behind} behind (as of the last fetch)")
        except GitError:
            pass
    ok, desc = timer.status()
    out.append(f"timer     {desc}")
    if st.problems:
        out.append("problems:")
        for p in st.problems.values():
            out.append(f"  ! {p['message']}" + (f"\n    {p['hint']}" if p.get("hint") else ""))
    else:
        out.append("problems  none")
    return "\n".join(out)


# ---- doctor -------------------------------------------------------------------------------------


def _commands_in(settings_obj: dict) -> Iterator[str]:
    hooks = settings_obj.get("hooks") or {}
    if isinstance(hooks, dict):
        for entries in hooks.values():
            for entry in entries if isinstance(entries, list) else []:
                for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                    if isinstance(h, dict) and h.get("command"):
                        yield h["command"]
    status_line = settings_obj.get("statusLine")
    if isinstance(status_line, dict) and status_line.get("command"):
        yield status_line["command"]


def _missing_paths(command: str) -> List[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return [t for t in tokens if t.startswith("/") and not Path(t).exists()]


def doctor(ctx: Context) -> Tuple[bool, str]:
    checks: List[Tuple[str, str, str]] = []  # (level, what, detail)

    def add(level: str, what: str, detail: str = "") -> None:
        checks.append((level, what, detail))

    git = Git(ctx.repo, ctx.git_name, ctx.git_email)
    add("ok", "config", f"host {ctx.host}, repo {ctx.paths.tilde(ctx.repo)}")
    if not ctx.git_email or not ctx.git_name:
        add("fail", "git identity", "no commit name/email in config; set them with `agent-sync init ... --force "
            "--git-name ... --git-email ...`")
    if git.has_remote():
        r = git.run("ls-remote", "-q", "origin", "HEAD", check=False, timeout=30)
        if r.returncode == 0:
            add("ok", "remote", git.remote_url())
        else:
            add("fail", "remote", f"can't reach {git.remote_url()}: {(r.stderr or '').strip()[:200]}")
    else:
        add("warn", "remote", "no remote; changes stay on this machine")

    pending = links.plan(ctx)
    if pending:
        add("warn", "links", "; ".join(a.describe() for a in pending))
    else:
        add("ok", "links", "skills, commands and agents all linked")

    try:
        gen = settings.generate(ctx)
        current = settings.read_current(ctx)
        if current is not None and jsonutil.same(current, gen):
            add("ok", "settings.json", "matches the repo")
        else:
            changed = jsonutil.leaf_paths(jsonutil.diff(current or {}, gen))
            add("warn", "settings.json", "differs from the repo in " + ", ".join(changed[:8]) + " (next sync fixes it)")
        missing = sorted({m for c in _commands_in(gen) for m in _missing_paths(c)})
        if missing:
            add("fail", "hook/statusLine commands", "missing on this machine: " + ", ".join(missing))
        plugins_file = ctx.paths.claude / "plugins" / "installed_plugins.json"
        installed = (jsonutil.load(plugins_file, {}) or {}).get("plugins", {}) if plugins_file.exists() else {}
        wanted = [p for p, on in (gen.get("enabledPlugins") or {}).items() if on]
        absent = [p for p in wanted if p not in installed]
        if absent:
            add("fail", "plugins", "enabled but not installed: " + ", ".join(absent)
                + " (claude plugin install <name>@<marketplace>)")
    except (settings.Unreadable, ValueError) as exc:
        add("fail", "settings.json", f"not valid JSON: {exc}")

    doc = claudemd.path(ctx)
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    if claudemd.BEGIN_PREFIX in text:
        add("ok", "CLAUDE.md", "imports the shared instructions")
    else:
        add("warn", "CLAUDE.md", "no agent-sync block yet (next sync adds it)")
    if claudemd.outside_text(text):
        add("warn", "CLAUDE.md", "has text outside managed blocks that won't sync")

    for req in ctx.hostfile.get("requires", []) or []:
        name = req.get("command") or req.get("path", "")
        present = shutil.which(name) if req.get("command") else Path(
            jsonutil.substitute(name, ctx.placeholders())).exists()
        if not present:
            add("fail", f"requires {name}", req.get("hint", ""))

    ok, desc = timer.status()
    add("ok" if ok else "warn", "timer", desc + ("" if ok else " (agent-sync timer install)"))

    st = State(ctx.paths.state_dir)
    for p in st.problems.values():
        add("fail", "problem", p["message"])

    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"{marks[level]}  {what}" + (f": {detail}" if detail else "") for level, what, detail in checks]
    return all(level != "fail" for level, _, _ in checks), "\n".join(lines)


def show_settings(ctx: Context, host: str = "") -> str:
    if host and host != ctx.host:
        hostfile = jsonutil.load(ctx.repo / "hosts" / f"{host}.json", {}) or {}
        base = jsonutil.load(settings.base_path(ctx), {}) or {}
        return json.dumps(jsonutil.merge(base, hostfile.get("settings", {}) or {}), indent=2)
    return json.dumps(settings.generate(ctx), indent=2)
