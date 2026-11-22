"""The few git operations sync needs, run non-interactively."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .context import AgentSyncError

NETWORK_TIMEOUT = 120


class GitError(Exception):
    def __init__(self, args: List[str], result: subprocess.CompletedProcess):
        self.result = result
        out = [l.strip() for l in (result.stderr or result.stdout or "").strip().splitlines() if l.strip()]
        detail = " ".join(out)[:400] if out else f"exit {result.returncode}"
        super().__init__(f"git {' '.join(args)} failed: {detail}")


class Git:
    def __init__(self, repo: Path, name: str = "", email: str = ""):
        self.repo = repo
        self.name = name
        self.email = email

    def run(self, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = ["git", "-C", str(self.repo), "-c", "core.quotepath=off"]
        if self.name:
            cmd += ["-c", f"user.name={self.name}"]
        if self.email:
            cmd += ["-c", f"user.email={self.email}"]
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_EDITOR="true")
        try:
            result = subprocess.run(
                cmd + list(args), capture_output=True, text=True, env=env, timeout=timeout
            )
        except FileNotFoundError:
            raise AgentSyncError("git isn't installed or isn't on PATH.")
        except subprocess.TimeoutExpired:
            raise GitError(list(args), subprocess.CompletedProcess(args, 124, "", "timed out"))
        if check and result.returncode != 0:
            raise GitError(list(args), result)
        return result

    def out(self, *args: str, timeout: int = 60) -> str:
        return self.run(*args, timeout=timeout).stdout.strip()

    # ---- facts ----------------------------------------------------------------------------------

    def branch(self) -> str:
        return self.out("rev-parse", "--abbrev-ref", "HEAD")

    def has_remote(self) -> bool:
        return "origin" in self.out("remote").split()

    def remote_url(self) -> str:
        return self.run("remote", "get-url", "origin", check=False).stdout.strip()

    def git_dir(self) -> Path:
        return Path(self.out("rev-parse", "--absolute-git-dir"))

    def busy(self) -> Optional[str]:
        """A rebase or merge left in progress (by a person, or a crash), or None."""
        git_dir = self.git_dir()
        for name, what in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge")):
            if (git_dir / name).exists():
                return what
        return None

    def has_commits(self) -> bool:
        return self.run("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0

    def remote_branch_exists(self, branch: str) -> bool:
        return self.run("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False).returncode == 0

    def ahead_behind(self, branch: str) -> Tuple[int, int]:
        if not self.remote_branch_exists(branch) or not self.has_commits():
            return (1 if self.has_commits() else 0), 0
        left, right = self.out("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}").split()
        return int(left), int(right)

    def dirty(self) -> List[str]:
        return [l for l in self.out("status", "--porcelain").splitlines() if l.strip()]

    def tracked(self) -> List[str]:
        """Paths in the index (after stage_all: what the next commit will contain)."""
        return [l for l in self.out("ls-files").splitlines() if l]

    def tracked_at_head(self) -> List[str]:
        if not self.has_commits():
            return []
        return [l for l in self.out("ls-tree", "-r", "--name-only", "HEAD").splitlines() if l]

    def staged_gitlinks(self) -> List[str]:
        """Paths this commit would add or change into nested git repositories (mode 160000), which git
        stores as empty links. Gitlinks already in HEAD are not reported again."""
        out = []
        for line in self.out("diff", "--cached", "--raw", "--no-renames").splitlines():
            meta, _, path = line.partition("\t")
            fields = meta.split()
            if len(fields) >= 5 and fields[1] == "160000" and fields[4][:1] in "AMT":
                out.append(path)
        return out

    # ---- changes --------------------------------------------------------------------------------

    def stage_all(self) -> List[Tuple[str, str]]:
        """git add -A, then the staged (status, path) pairs."""
        self.run("add", "-A")
        pairs = []
        for line in self.out("diff", "--cached", "--name-status", "--no-renames").splitlines():
            status, _, path = line.partition("\t")
            if path:
                pairs.append((status[:1], path))
        return pairs

    def staged_patch(self) -> str:
        return self.run("diff", "--cached", "-U0", "--no-color", "--no-renames", "--no-ext-diff").stdout

    def staged_sizes(self) -> List[Tuple[str, int]]:
        sizes = []
        for line in self.out("diff", "--cached", "--name-only", "--diff-filter=AM").splitlines():
            p = self.repo / line
            if p.is_file():
                sizes.append((line, p.stat().st_size))
        return sizes

    def unstage(self) -> None:
        if self.has_commits():
            self.run("reset", "-q")
        else:
            self.run("rm", "-r", "-q", "--cached", ".", check=False)

    def commit(self, message: str) -> None:
        self.run("commit", "-q", "--no-verify", "-m", message)

    # ---- network --------------------------------------------------------------------------------

    def fetch(self) -> None:
        self.run("fetch", "-q", "origin", timeout=NETWORK_TIMEOUT)

    def rebase_onto_remote(self, branch: str, fork_point: bool = False) -> Optional[List[str]]:
        """Rebase local commits onto origin/<branch>. Returns conflicted paths (rebase aborted) or None.

        fork_point: drop commits the remote's rewritten history no longer has, instead of replaying them.
        """
        args = ["rebase", "-q"] + (["--fork-point"] if fork_point else []) + [f"origin/{branch}"]
        result = self.run(*args, check=False, timeout=NETWORK_TIMEOUT)
        if result.returncode == 0:
            return None
        conflicted = self.run("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        if self.busy():
            self.run("rebase", "--abort", check=False)
        if not conflicted:  # refused to start (dirty tree, bad ref...): an error, not a conflict
            raise GitError(["rebase", f"origin/{branch}"], result)
        return conflicted

    def push(self, branch: str) -> bool:
        """Push; False if the remote moved on (someone else pushed first)."""
        result = self.run("push", "-q", "-u", "origin", f"HEAD:{branch}", check=False, timeout=NETWORK_TIMEOUT)
        if result.returncode == 0:
            return True
        text = (result.stderr or "") + (result.stdout or "")
        if "rejected" in text or "fetch first" in text or "non-fast-forward" in text:
            return False
        raise GitError(["push", "origin", branch], result)
