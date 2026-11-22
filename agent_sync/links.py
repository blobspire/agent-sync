"""Skills, commands and agents: one symlink per entry from ~/.claude/<kind>/<name> into the repo.

Because each entry is a link, an agent that edits ~/.claude/skills/x/SKILL.md is editing the repo's
working tree, and the next sync commits it. A new real folder or file an agent creates there is adopted:
moved into the repo, with a link left in its place.

Never touched: hidden names, claude.ai's skills/synced folder, names in the manifest's ignore list, and
links that point somewhere other than the repo (another tool's skill).
"""

from __future__ import annotations

import filecmp
import os
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .context import KINDS, Context
from .state import Backups

QUIET_SECONDS = 120  # don't adopt something an agent is still writing


@dataclass
class Action:
    kind: str
    name: str
    op: str  # link, relink, unlink, adopt, replace, take-local, collision, foreign, nested-git
    detail: str = ""

    @property
    def rel(self) -> str:
        return f"{self.kind}/{self.name}"

    @property
    def is_problem(self) -> bool:
        return self.op in ("collision", "foreign", "nested-git")

    def describe(self) -> str:
        words = {
            "link": "link",
            "relink": "relink",
            "unlink": "remove link",
            "adopt": "adopt into repo",
            "replace": "replace local copy with link",
            "take-local": "copy local version into repo, then link",
            "collision": "CONFLICT",
            "foreign": "SKIPPED",
            "nested-git": "NOT SHARED",
        }[self.op]
        return f"{words:<14} {self.rel}" + (f"  ({self.detail})" if self.detail else "")


def _entries(path: Path) -> List[str]:
    try:
        return sorted(n for n in os.listdir(path) if not n.startswith("."))
    except FileNotFoundError:
        return []


def _real(path: Path) -> str:
    return os.path.realpath(str(path))


def _points_into(link: Path, root: Path) -> bool:
    target, root_real = _real(link), _real(root)
    return target == root_real or target.startswith(root_real + os.sep)


def _fold(name: str) -> str:
    """Names macOS treats as the same file (APFS ignores case and Unicode normalization by default)."""
    return unicodedata.normalize("NFC", name).casefold()


def _has_git(path: Path) -> bool:
    if not path.is_dir():
        return False
    for root, dirs, files in os.walk(path):
        if ".git" in dirs or ".git" in files:
            return True
    return False


def _adoptable(kind: str, path: Path) -> bool:
    if kind == "skills":
        return path.is_dir() and (path / "SKILL.md").is_file()
    return path.is_dir() or (path.is_file() and path.suffix == ".md")


def _newest_mtime(path: Path) -> float:
    newest = path.lstat().st_mtime
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except FileNotFoundError:
                    pass
    return newest


def _files(path: Path) -> dict:
    out = {}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            if name == ".DS_Store":
                continue
            full = os.path.join(root, name)
            out[os.path.relpath(full, path)] = full
    return out


def same_tree(a: Path, b: Path) -> bool:
    if a.is_file() and b.is_file():
        return filecmp.cmp(a, b, shallow=False)
    if not (a.is_dir() and b.is_dir()):
        return False
    fa, fb = _files(a), _files(b)
    if set(fa) != set(fb):
        return False
    return all(filecmp.cmp(fa[k], fb[k], shallow=False) for k in fa)


def plan(ctx: Context, adopt_all: bool = False, prefer: str = "repo") -> List[Action]:
    """What apply() would do. adopt_all: first install on a machine (replace or take local copies)."""
    actions: List[Action] = []
    for kind in KINDS:
        repo_dir, dest_dir = ctx.repo / kind, ctx.paths.claude / kind
        if dest_dir.is_symlink():
            # Linking inside a linked folder would rewrite whatever it points at (another tool's tree,
            # or the repo itself). Leave the whole kind alone.
            actions.append(Action(kind, "", "foreign", f"~/.claude/{kind} is itself a link to {os.readlink(dest_dir)}"))
            continue
        repo_names = _entries(repo_dir)
        repo_folded = {_fold(n) for n in repo_names}
        for name in repo_names:
            src, dst = repo_dir / name, dest_dir / name
            if not ctx.allowed(kind, name):
                if dst.is_symlink() and _points_into(dst, repo_dir):
                    actions.append(Action(kind, name, "unlink", "not used on this host"))
                continue
            if dst.is_symlink():
                if _real(dst) == _real(src):
                    continue
                if _points_into(dst, ctx.repo):
                    actions.append(Action(kind, name, "relink"))
                else:
                    actions.append(Action(kind, name, "foreign", f"links to {os.readlink(dst)}"))
            elif dst.exists():
                if same_tree(dst, src):
                    actions.append(Action(kind, name, "replace", "identical local copy"))
                elif adopt_all and prefer == "local":
                    actions.append(Action(kind, name, "take-local", "local copy differs; it wins"))
                elif adopt_all:
                    actions.append(Action(kind, name, "replace", "local copy differs; backed up"))
                else:
                    actions.append(Action(kind, name, "collision", "a local copy differs from the repo's"))
            else:
                actions.append(Action(kind, name, "link"))
        for name in _entries(dest_dir):
            if name in repo_names or _fold(name) in repo_folded:
                continue  # same name, or one the filesystem treats as the same: handled (or reported) above
            dst = dest_dir / name
            if dst.is_symlink():
                if _points_into(dst, repo_dir):
                    actions.append(Action(kind, name, "unlink", "removed from the repo"))
                continue
            if name in ctx.ignored(kind) or not _adoptable(kind, dst):
                continue
            if not adopt_all and _newest_mtime(dst) > time.time() - QUIET_SECONDS:
                continue  # still being written; adopt on a later run
            if _has_git(dst):
                actions.append(Action(kind, name, "nested-git",
                                      "has its own .git folder (installed with git clone?)"))
                continue
            actions.append(Action(kind, name, "adopt"))
    return actions


def apply(ctx: Context, actions: List[Action], backups: Backups) -> List[str]:
    """Carry out planned actions. Returns problems (one line each) for status and the session hook."""
    problems: List[str] = []
    for a in actions:
        src = ctx.repo / a.kind / a.name
        dst = ctx.paths.claude / a.kind / a.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if a.op == "link":
                os.symlink(src, dst)
            elif a.op == "relink":
                os.unlink(dst)
                os.symlink(src, dst)
            elif a.op == "unlink":
                os.unlink(dst)
            elif a.op == "replace":
                backups.move(dst)
                os.symlink(src, dst)
            elif a.op == "take-local":
                if src.is_dir() and not src.is_symlink():
                    shutil.rmtree(src)
                elif src.exists() or src.is_symlink():
                    src.unlink()
                if dst.is_dir():
                    shutil.copytree(dst, src, symlinks=True)
                else:
                    shutil.copy2(dst, src)
                backups.move(dst)
                os.symlink(src, dst)
            elif a.op == "adopt":
                if src.exists() or src.is_symlink():
                    problems.append(f"Didn't adopt {ctx.paths.tilde(dst)}: the repo already has {a.rel} "
                                    "(maybe under a name differing only in case).")
                    continue
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                os.symlink(src, dst)
            elif a.op == "collision":
                problems.append(
                    f"{ctx.paths.tilde(dst)} is a real copy that differs from the shared one in the repo "
                    f"({a.rel}). Merge what's worth keeping into {ctx.paths.tilde(ctx.paths.repo_link / a.rel)}, "
                    f"then delete the local copy; the next sync links it."
                )
            elif a.op == "foreign":
                if a.name:
                    problems.append(f"{ctx.paths.tilde(dst)} {a.detail}, so the shared {a.rel} isn't installed here.")
                else:
                    problems.append(f"{a.detail}, so agent-sync doesn't manage {a.kind} on this machine. "
                                    f"Make ~/.claude/{a.kind} a real folder to share them.")
            elif a.op == "nested-git":
                problems.append(
                    f"{ctx.paths.tilde(dst)} {a.detail}, so it isn't shared: git would store it as an empty "
                    "submodule. Delete its .git folder to share it as plain files, or add its name to "
                    "\"ignore\" in agent-sync.json to keep it on this machine only."
                )
        except OSError as exc:
            problems.append(f"Couldn't {a.op} {a.rel}: {exc}")
    return problems


def ensure_repo_link(ctx: Context, dry_run: bool = False) -> List[str]:
    """~/.claude/agent-config -> the repo, so instructions can name a stable path."""
    link = ctx.paths.repo_link
    if link.is_symlink() and _real(link) == _real(ctx.repo):
        return []
    if link.exists() and not link.is_symlink():
        return [f"{ctx.paths.tilde(link)} exists and isn't agent-sync's link; move it aside."]
    if not dry_run:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        os.symlink(ctx.repo, link)
    return []


def materialize(ctx: Context) -> List[str]:
    """Uninstall: replace every link into the repo with a real copy. Returns what was copied."""
    done = []
    for kind in KINDS:
        dest_dir = ctx.paths.claude / kind
        for name in _entries(dest_dir):
            dst = dest_dir / name
            if dst.is_symlink() and _points_into(dst, ctx.repo / kind):
                src = Path(_real(dst))
                dst.unlink()
                if src.is_dir():
                    shutil.copytree(src, dst, symlinks=True)
                elif src.exists():
                    shutil.copy2(src, dst)
                done.append(f"{kind}/{name}")
    if ctx.paths.repo_link.is_symlink():
        ctx.paths.repo_link.unlink()
    return done
