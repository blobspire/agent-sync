"""Secret detection, used before every commit and when capturing settings.

Deliberately simple: well-known token shapes plus "<name containing key/secret/token/password> = <long
mixed value>". A line containing `agent-sync: allow` is never flagged. Findings name the file, line and
kind of secret, never the secret itself.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

ALLOW = "agent-sync: allow"

_VALUE = r"[A-Za-z0-9/+_\-=.]"
PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    (
        "assigned secret",
        re.compile(
            r"(?i)[A-Za-z0-9_]*(?:api[_-]?key|secret|token|passw(?:or)?d)[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?"
            r"(?!\$|\{\{|<)"
            rf"(?={_VALUE}*[0-9])(?={_VALUE}*[A-Za-z]){_VALUE}{{20,}}"
        ),
    ),
]

# Environment variable names that hold secrets even when the value has no recognizable shape.
SECRET_NAME = re.compile(r"(?i)(token|secret|passw(or)?d|api_?key|private_?key|credential)")


def find(text: str) -> List[str]:
    """Kinds of secrets found in `text` (empty if none)."""
    if ALLOW in text:
        return []
    return [name for name, pattern in PATTERNS if pattern.search(text)]


def scan_added_lines(diff_text: str) -> List[str]:
    """Scan the added lines of a `git diff -U0` and return findings like 'skills/x/a.sh:12: GitHub token'."""
    findings: List[str] = []
    path = "?"
    line_no = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
            continue
        if line.startswith("+"):
            for kind in find(line[1:]):
                findings.append(f"{path}:{line_no}: {kind}")
            line_no += 1
    return findings


def is_secret_setting(path: Iterable[str], value: object) -> bool:
    """Whether a settings.json leaf should stay local instead of being committed.

    Any value with a known secret shape counts. A name like *_TOKEN counts only inside "env", so keys
    such as "apiKeyHelper" (a command path) are not mistaken for secrets.
    """
    path = list(path)
    if isinstance(value, str) and find(value):
        return True
    if isinstance(value, list) and any(isinstance(v, str) and find(v) for v in value):
        return True
    return len(path) >= 2 and path[-2] == "env" and bool(SECRET_NAME.search(path[-1]))
