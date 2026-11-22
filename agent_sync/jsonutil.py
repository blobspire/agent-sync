"""JSON helpers: overlay merge, minimal diff, common subset, path placeholders, atomic writes."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, List, Sequence, Tuple


def same(a: Any, b: Any) -> bool:
    """Strict JSON equality (Python's == treats True == 1 and ignores nothing else we care about)."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def merge(base: Any, overlay: Any) -> Any:
    """Apply `overlay` on top of `base`.

    Dicts merge key by key, recursively. A None value in the overlay deletes the key. Anything else
    (strings, numbers, lists) replaces the base value outright.
    """
    if not isinstance(overlay, dict):
        return copy.deepcopy(overlay)
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in overlay.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict):
            out[key] = merge(out.get(key), value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def diff(base: Any, desired: Any) -> dict:
    """The smallest overlay such that merge(base, overlay) == desired (both dicts)."""
    if not isinstance(base, dict):
        base = {}
    out: dict = {}
    for key in base:
        if key not in desired:
            out[key] = None
    for key, value in desired.items():
        if key not in base:
            out[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(base[key], dict):
            sub = diff(base[key], value)
            if sub:
                out[key] = sub
        elif not same(base[key], value):
            # Includes a dict replacing a non-dict: merge() then starts from {} and lands it as-is.
            out[key] = copy.deepcopy(value)
    return out


def common(objs: Sequence[dict]) -> dict:
    """Keys (recursively) that have the same value in every dict of `objs`."""
    if not objs:
        return {}
    first = objs[0]
    out: dict = {}
    for key, value in first.items():
        if not all(key in o for o in objs[1:]):
            continue
        values = [o[key] for o in objs]
        if all(isinstance(v, dict) for v in values):
            sub = common(values)
            if sub:
                out[key] = sub
        elif all(same(v, value) for v in values):
            out[key] = copy.deepcopy(value)
    return out


def leaf_paths(overlay: Any, prefix: str = "") -> List[str]:
    """Dotted key paths an overlay touches, for human-readable reports."""
    if not isinstance(overlay, dict) or not overlay:
        return [prefix] if prefix else []
    paths: List[str] = []
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            paths.extend(leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


# ---- placeholders -------------------------------------------------------------------------------

Mapping = Sequence[Tuple[str, str]]  # (placeholder, real value), e.g. ("{{HOME}}", "/home/me")


def substitute(obj: Any, mapping: Mapping) -> Any:
    """Replace placeholders with real values in every string value (keys are left alone)."""
    if isinstance(obj, dict):
        return {k: substitute(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, mapping) for v in obj]
    if isinstance(obj, str):
        for placeholder, real in mapping:
            obj = obj.replace(placeholder, real)
    return obj


def unsubstitute(obj: Any, mapping: Mapping) -> Any:
    """Replace real paths with placeholders, longest value first, only at path boundaries.

    "/home/me" becomes "{{HOME}}" in "/home/me/bin" but not in "/home/meg/bin".
    """
    patterns = [
        (placeholder, re.compile(r"(?<![\w./-])" + re.escape(real) + r"(?![\w.-])"))
        for placeholder, real in sorted(mapping, key=lambda m: len(m[1]), reverse=True)
        if real
    ]

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str):
            for placeholder, pattern in patterns:
                value = pattern.sub(lambda _m, p=placeholder: p, value)
        return value

    return walk(obj)


# ---- files --------------------------------------------------------------------------------------


def load(path: Path, default: Any = None) -> Any:
    """Parse a JSON file. Missing file -> default. Malformed file raises ValueError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return copy.deepcopy(default)
    if not text.strip():
        return copy.deepcopy(default)
    return json.loads(text)


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file in the same directory and rename, keeping the existing file mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save(path: Path, obj: Any, mode: int | None = None) -> None:
    write_atomic(path, dumps(obj), mode)


def unique(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
