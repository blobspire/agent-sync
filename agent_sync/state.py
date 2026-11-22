"""Per-machine state in ~/.local/state/agent-sync: last run, open problems, lock, log, backups."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import jsonutil

LOG_LIMIT = 512 * 1024


def now() -> float:
    return time.time()


def stamp(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else now(), timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class State:
    """state.json: a small dict of facts other commands (status, the session hook) report from.

    Keys:
      last_run, last_ok     timestamps of the last sync attempt / last sync that reached the remote
      problems              {key: {"since": ts, "message": str, "hint": str}} still open
      notified              {key: ts} problems already sent to the notify command
    """

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        try:
            self.data = jsonutil.load(self.path, {}) or {}
        except ValueError:
            self.data = {}  # a corrupt state file only loses history; start fresh
        self.data.setdefault("problems", {})
        self.data.setdefault("notified", {})

    def save(self) -> None:
        jsonutil.save(self.path, self.data, mode=0o600)

    # problems are keyed so that the same problem keeps its original "since" time across runs
    def open_problem(self, key: str, message: str, hint: str = "") -> None:
        existing = self.data["problems"].get(key)
        since = existing["since"] if existing else now()
        self.data["problems"][key] = {"since": since, "message": message, "hint": hint}

    def close_problem(self, key: str) -> None:
        self.data["problems"].pop(key, None)
        self.data["notified"].pop(key, None)

    def close_problems(self, prefix: str) -> None:
        for key in [k for k in self.data["problems"] if k.startswith(prefix)]:
            self.close_problem(key)

    @property
    def problems(self) -> dict:
        return self.data["problems"]


@contextmanager
def lock(state_dir: Path, wait: bool) -> Iterator[bool]:
    """Exclusive lock so two syncs (timer + an agent) never run at once. Yields False if busy."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(state_dir / "lock", "w")
    try:
        deadline = now() + (120 if wait else 0)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if now() >= deadline:
                    yield False
                    return
                time.sleep(0.5)
        yield True
    finally:
        fh.close()


def log(state_dir: Path, message: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "sync.log"
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, state_dir / "sync.log.1")
    except FileNotFoundError:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        for line in message.rstrip("\n").splitlines():
            fh.write(f"{stamp()} {line}\n")


class Backups:
    """One timestamped folder per run that replaces anything, created only when first used."""

    def __init__(self, state_dir: Path, claude_dir: Path):
        self.root = state_dir / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.claude_dir = claude_dir
        self.used = False

    def _dest(self, path: Path) -> Path:
        if not self.used:
            base, n = self.root, 1
            while self.root.exists():
                self.root = base.with_name(f"{base.name}-{n}")
                n += 1
        try:
            rel = Path(path).relative_to(self.claude_dir)
            dest = self.root / "claude" / rel
        except ValueError:
            dest = self.root / "other" / Path(path).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.used = True
        return dest

    def move(self, path: Path) -> Path:
        dest = self._dest(path)
        shutil.move(str(path), str(dest))
        return dest

    def copy(self, path: Path) -> Path:
        dest = self._dest(path)
        if Path(path).is_dir() and not Path(path).is_symlink():
            shutil.copytree(path, dest, symlinks=True)
        else:
            shutil.copy2(path, dest, follow_symlinks=False)
        return dest
