"""Read-only checks on the shared instructions, used by `doctor`.

They look for the ways a shared setup decays: settings copied from another machine, note links that point
nowhere, project memories that duplicate a shared note, and an always-loaded set that keeps growing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator, List, Tuple

# Claude Code's guidance is to keep each always-loaded instruction file under about 200 lines.
ALWAYS_LOADED_LINE_LIMIT = 200

_HOME_PATH = re.compile(r"(?<![\w.~])/(?:Users|home)/[A-Za-z0-9._-]+")
_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_INDEX_ENTRY = re.compile(r"\]\(([^)\s]+\.md)\)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")


def _prose(text: str) -> str:
    """Text with inline code removed, so examples like `[Title](file.md)` aren't read as real entries."""
    return _CODE_SPAN.sub("", text)


def _strings(value: object, path: str = "") -> Iterator[Tuple[str, str]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def foreign_home_paths(settings: dict, home: str, allowed: Iterable[str] = ()) -> List[Tuple[str, str]]:
    """(key path, offending path) for every home directory in the settings that isn't this machine's.

    A path under another user's or another platform's home (`/Users/x` on Linux, `/home/x` on macOS) usually
    means the settings, or text inside them, were copied from a different computer. `allowed` lists exact
    strings to skip, such as agent-sync's own command path, which agent-sync writes itself.
    """
    own = home.rstrip("/")
    skip = [a for a in allowed if a]
    found = []
    for key, text in _strings(settings):
        for a in skip:
            text = text.replace(a, "")
        for match in _HOME_PATH.finditer(text):
            if match.group(0) != own:
                found.append((key, match.group(0)))
    return found


def note_names(repo: Path) -> List[str]:
    notes = repo / "notes"
    if not notes.is_dir():
        return []
    return sorted(p.stem for p in notes.glob("*.md") if p.name != "INDEX.md")


def dangling_links(repo: Path) -> List[Tuple[str, str]]:
    """(note file, link target) for every [[link]] in a note that matches no note."""
    names = set(note_names(repo))
    found = []
    for name in note_names(repo):
        text = _prose((repo / "notes" / f"{name}.md").read_text(encoding="utf-8", errors="replace"))
        for target in _LINK.findall(text):
            if target.strip() not in names:
                found.append((f"{name}.md", target.strip()))
    return found


def index_problems(repo: Path) -> Tuple[List[str], List[str]]:
    """(INDEX entries whose file is missing, notes that INDEX doesn't list)."""
    index = repo / "notes" / "INDEX.md"
    if not index.exists():
        return [], []
    listed = _INDEX_ENTRY.findall(_prose(index.read_text(encoding="utf-8", errors="replace")))
    missing = [f for f in listed if not (repo / "notes" / f).exists()]
    unlisted = [f"{n}.md" for n in note_names(repo) if f"{n}.md" not in listed]
    return missing, unlisted


def duplicate_memories(claude: Path, repo: Path) -> List[Tuple[str, str, bool]]:
    """(memory file, matching note, identical?) for project memories named like a shared note.

    A note promoted from one project's memory into the shared notes should leave that project: otherwise
    sessions there see it twice, and the two copies drift apart.
    """
    names = set(note_names(repo))
    found = []
    projects = claude / "projects"
    if not names or not projects.is_dir():
        return found
    for mem in sorted(projects.glob("*/memory/*.md")):
        if mem.name == "MEMORY.md" or mem.stem not in names:
            continue
        note = repo / "notes" / mem.name
        same = mem.read_bytes() == note.read_bytes()
        found.append((f"{mem.parent.parent.name}/memory/{mem.name}", mem.name, same))
    return found


def always_loaded(files: List[Path]) -> Tuple[int, int, List[Tuple[Path, int]]]:
    """(total lines, total bytes, per-file lines) for the files every session loads."""
    per_file = []
    lines = size = 0
    for f in files:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        n = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        per_file.append((f, n))
        lines += n
        size += len(text.encode("utf-8"))
    return lines, size, per_file
