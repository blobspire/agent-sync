"""~/.claude/CLAUDE.md: agent-sync owns one marked block that imports the shared instructions.

CLAUDE.md stays a regular file. Other tools rewrite it too (some by writing a new file and renaming it
over the old one, which would replace a symlink), so agent-sync only manages its own block and leaves
every other block alone:

    <!-- BEGIN agent-sync (...) -->
    @~/.claude/agent-config/CLAUDE.md
    @~/.claude/agent-config/hosts/<host>.md
    <!-- END agent-sync -->
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from . import jsonutil
from .context import Context
from .state import Backups

BEGIN_PREFIX = "<!-- BEGIN agent-sync"
BEGIN = "<!-- BEGIN agent-sync (managed by agent-sync: edit the files it imports, not this block) -->"
END = "<!-- END agent-sync -->"
ANY_BLOCK = re.compile(r"<!-- BEGIN ([\w.-]+)\b[^\n]*-->\n.*?<!-- END \1 -->[ \t]*\n?", re.S)
OUR_BLOCK = re.compile(r"<!-- BEGIN agent-sync\b[^\n]*-->[ \t]*\r?\n(.*?)<!-- END agent-sync -->[ \t]*(?:\r?\n)?", re.S)
# A relative @import: "@" at a line start or after whitespace, not ~/ or /, and path-like (has . or /).
RELATIVE_IMPORT = re.compile(r"(?m)(?:^|(?<=\s))@(?![~/])([^\s`]*[./][^\s`]*)")


class Broken(Exception):
    """agent-sync's BEGIN/END markers are missing, duplicated or interleaved with another block."""


def rebase_imports(text: str, base: str) -> str:
    """Point relative @imports at `base`, so moving text to another file keeps them working."""
    return RELATIVE_IMPORT.sub(lambda m: f"@{base}/{m.group(1)}", text)


def path(ctx: Context) -> Path:
    return ctx.paths.claude / "CLAUDE.md"


def block(ctx: Context, with_host_file: bool) -> str:
    link = ctx.paths.repo_link
    lines = [f"@{ctx.paths.tilde(link / 'CLAUDE.md')}"]
    if with_host_file:
        lines.append(f"@{ctx.paths.tilde(link / 'hosts' / (ctx.host + '.md'))}")
    return BEGIN + "\n" + "\n".join(lines) + "\n" + END


def _span(doc: str):
    """(start, end) of agent-sync's block, or None if there is none.

    Raises Broken unless there is exactly one BEGIN and one END forming a clean block: pairing a stray
    BEGIN with a later END would swallow everything between them, other tools' blocks included.
    """
    begins, ends = doc.count(BEGIN_PREFIX), doc.count(END)
    if begins == 0 and ends == 0:
        return None
    m = OUR_BLOCK.search(doc)
    if begins != 1 or ends != 1 or m is None or "<!-- BEGIN " in m.group(1) or "<!-- END " in m.group(1):
        raise Broken(
            f"its agent-sync markers are damaged ({begins} BEGIN and {ends} END line(s), "
            "or another block sits between them)"
        )
    return m.start(), m.end()


def upsert(doc: str, blk: str) -> str:
    span = _span(doc)
    if span:
        i, j = span
        return doc[:i] + blk + "\n" + doc[j:]
    if doc and not doc.endswith("\n"):
        doc += "\n"
    if doc:
        doc += "\n"
    return doc + blk + "\n"


def remove(doc: str) -> str:
    span = _span(doc)
    if not span:
        return doc
    i, j = span
    out = doc[:i].rstrip("\n") + ("\n" if doc[:i].strip() else "") + doc[j:].lstrip("\n")
    return out if out.strip() else ""


def outside_text(doc: str) -> str:
    """Text that isn't inside any tool's managed block."""
    return ANY_BLOCK.sub("", doc).strip()


def only_blocks(doc: str) -> str:
    blocks = [m.group(0).rstrip() for m in ANY_BLOCK.finditer(doc)]
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def apply(ctx: Context, backups: Backups, adopt: bool = False, dry_run: bool = False) -> List[str]:
    """Insert or refresh the block. With adopt, move unmanaged text into hosts/<host>.md first.

    Returns notes for the report.
    """
    p = path(ctx)
    doc = p.read_text(encoding="utf-8") if p.exists() else ""
    new = doc
    notes: List[str] = []
    has_host_file = ctx.host_md.exists()
    if adopt:
        extra = outside_text(doc)
        if extra:
            notes.append(
                f"moved {len(extra.splitlines())} line(s) of CLAUDE.md that no tool manages into "
                f"hosts/{ctx.host}.md (move any that belong on every machine to the shared CLAUDE.md)"
            )
            # Relative imports resolved from ~/.claude; in hosts/<host>.md they'd resolve from there instead.
            extra = rebase_imports(extra, ctx.paths.tilde(ctx.paths.claude))
            if not dry_run:
                existing = ctx.host_md.read_text(encoding="utf-8") if ctx.host_md.exists() else ""
                joined = (existing.rstrip() + "\n\n" if existing.strip() else "") + extra + "\n"
                ctx.host_md.parent.mkdir(parents=True, exist_ok=True)
                ctx.host_md.write_text(joined, encoding="utf-8")
            has_host_file = True
            new = only_blocks(doc)
    had_block = _span(doc) is not None  # raises Broken before anything is written
    new = upsert(new, block(ctx, has_host_file))
    if new != doc:
        notes.append("CLAUDE.md: " + ("updated" if had_block else "added") + " the agent-sync block")
        if not dry_run:
            if doc and backups is not None:
                backups.copy(p)
            jsonutil.write_atomic(p, new)
    return notes


def strip(ctx: Context) -> bool:
    """Uninstall: remove the block, inlining what it imported so behaviour carries on. True if changed."""
    p = path(ctx)
    if not p.exists():
        return False
    doc = p.read_text(encoding="utf-8")
    if _span(doc) is None:  # raises Broken: leave a damaged file for a person
        return False
    parts = []
    for f in (ctx.repo / "CLAUDE.md", ctx.host_md):
        if f.exists():
            text = f.read_text(encoding="utf-8")
            # Relative imports in these files pointed into the repo; keep them pointing there.
            text = rebase_imports(text, str(f.parent))
            parts.append(text.strip())
    inlined = "\n\n".join(p_ for p_ in parts if p_)
    i, j = _span(doc)
    new = doc[:i] + (inlined + "\n" if inlined else "") + doc[j:]
    jsonutil.write_atomic(p, new if new.strip() else "")
    return True
