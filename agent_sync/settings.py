"""settings.json: generated from the repo, with local changes written back to this host's overlay.

    settings.json = base.json  <-  hosts/<host>.json "settings"  <-  local secrets  <-  agent-sync's hook

Placeholders ({{HOME}}, {{CONFIG}}, {{AGENT_SYNC}}) are replaced with this machine's paths.

Claude Code itself writes settings.json (/model, /config, enabling a plugin). Before regenerating, the
difference between what agent-sync last wrote and what's there now is folded into this host's overlay,
never the shared base, so those changes stick on this machine only. Secret-looking values go to a
local-only file in the state directory instead of the repo.
"""

from __future__ import annotations

import copy
import json
import shlex
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import jsonutil, scan
from .context import Context
from .state import Backups

HOOK_MARK = "hook session-start"


class Unreadable(Exception):
    """A settings file that must be read (settings.json, base.json, the local secrets) isn't valid JSON."""

    def __init__(self, path: Path, detail: str):
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"{path}: {detail}")


# ---- paths --------------------------------------------------------------------------------------


def settings_path(ctx: Context) -> Path:
    return ctx.paths.claude / "settings.json"


def last_path(ctx: Context) -> Path:
    return ctx.paths.state_dir / "settings.last.json"


def local_path(ctx: Context) -> Path:
    return ctx.paths.state_dir / "local-settings.json"


def base_path(ctx: Context) -> Path:
    return ctx.repo / "settings" / "base.json"


def read_current(ctx: Context) -> Optional[dict]:
    """settings.json as a dict; None if missing; raises Unreadable if it isn't valid JSON."""
    try:
        data = jsonutil.load(settings_path(ctx), None)
    except ValueError as exc:
        raise Unreadable(settings_path(ctx), str(exc))
    if data is not None and not isinstance(data, dict):
        raise Unreadable(settings_path(ctx), "top level is not an object")
    return data


def _load(path: Path) -> dict:
    """A settings source: missing means empty, but a broken file is an error. Treating it as empty would
    silently drop every shared setting (permissions and hooks included) on every machine."""
    try:
        data = jsonutil.load(path, None)
    except ValueError as exc:
        raise Unreadable(path, str(exc))
    if data is None:
        return {}
    if not isinstance(data, dict):  # checked before any truthiness test: `[]` must not pass as `{}`
        raise Unreadable(path, "top level is not an object")
    return data


# ---- agent-sync's own SessionStart hook ---------------------------------------------------------


def _is_tool_hook_object(hook: Any) -> bool:
    cmd = hook.get("command", "") if isinstance(hook, dict) else ""
    return isinstance(cmd, str) and "agent-sync" in cmd and HOOK_MARK in cmd


def _is_tool_hook(entry: Any) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(_is_tool_hook_object(h) for h in hooks)


def add_tool_hook(settings: dict, ctx: Context) -> dict:
    if ctx.manifest.get("session_hook", True) is False:
        return settings
    out = copy.deepcopy(settings)
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return settings
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        return settings
    if not any(_is_tool_hook(e) for e in entries):
        command = f"{shlex.quote(ctx.command)} {HOOK_MARK}"
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return out


def strip_tool_hook(settings: dict) -> dict:
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get("SessionStart"), list):
        kept = []
        for entry in hooks["SessionStart"]:
            if _is_tool_hook(entry):
                # Remove only agent-sync's hook; anything someone added next to it in the same entry stays.
                rest = [h for h in entry["hooks"] if not _is_tool_hook_object(h)]
                if rest:
                    kept.append(dict(entry, hooks=rest))
            else:
                kept.append(entry)
        if kept:
            hooks["SessionStart"] = kept
        else:
            del hooks["SessionStart"]
        if not hooks:
            del out["hooks"]
    return out


# ---- secrets ------------------------------------------------------------------------------------


def split_secrets(obj: dict, path: Tuple[str, ...] = ()) -> Tuple[dict, dict]:
    """Split a settings dict (or overlay) into (shareable, local-only secrets)."""
    shared: dict = {}
    secret: dict = {}
    for key, value in obj.items():
        here = path + (key,)
        if isinstance(value, dict) and value:
            sub_shared, sub_secret = split_secrets(value, here)
            if sub_shared or not sub_secret:
                shared[key] = sub_shared
            if sub_secret:
                secret[key] = sub_secret
        elif scan.is_secret_setting(here, value):
            secret[key] = value
        else:
            shared[key] = value
    return shared, secret


# ---- generate / capture / write -----------------------------------------------------------------


def effective(ctx: Context) -> dict:
    """This host's settings in placeholder form (base + overlay), without local secrets or the hook."""
    overlay = ctx.hostfile.get("settings", {})
    if overlay is None:
        overlay = {}
    if not isinstance(overlay, dict):  # a list here would replace every setting
        raise Unreadable(ctx.hostfile_path, '"settings" must be a JSON object')
    return jsonutil.merge(_load(base_path(ctx)), overlay)


def generate(ctx: Context, local: Optional[dict] = None) -> dict:
    real = jsonutil.substitute(effective(ctx), ctx.placeholders())
    real = jsonutil.merge(real, _load(local_path(ctx)) if local is None else local)
    return add_tool_hook(real, ctx)


def _set_overlay(ctx: Context, target: dict, save: bool = True) -> None:
    overlay = jsonutil.diff(_load(base_path(ctx)), target)
    if overlay:
        ctx.hostfile["settings"] = overlay
    else:
        ctx.hostfile.pop("settings", None)
    if save:
        ctx.save_hostfile()


def capture(ctx: Context, dry_run: bool = False) -> List[str]:
    """Fold changes made to settings.json since agent-sync last wrote it into this host's overlay.

    Returns the dotted paths that changed. Raises Unreadable if settings.json is mid-edit or broken.
    """
    current = read_current(ctx)
    try:
        last = jsonutil.load(last_path(ctx), None)
    except ValueError:
        last = None  # corrupt record of the last write: regenerate (with a backup) instead of guessing
    if current is None or not isinstance(last, dict):
        return []
    cur, prev = strip_tool_hook(current), strip_tool_hook(last)
    if jsonutil.same(cur, prev):
        return []
    delta = jsonutil.diff(prev, cur)
    if dry_run:
        return jsonutil.leaf_paths(delta)
    shared, secret = split_secrets(delta)
    # Every value the delta touches is decided afresh: a secret that was deleted or replaced by a plain
    # value must leave the local file, or it would be merged back in on every sync.
    local = _load(local_path(ctx))
    for path, _ in _leaves(delta):
        _drop(local, path)
    local = jsonutil.merge(local, secret)
    if local:
        jsonutil.save(local_path(ctx), local, mode=0o600)
    else:
        local_path(ctx).unlink(missing_ok=True)
    if shared:
        placeholder_delta = jsonutil.unsubstitute(shared, ctx.placeholders())
        _set_overlay(ctx, jsonutil.merge(effective(ctx), placeholder_delta))
    return jsonutil.leaf_paths(delta)


def _leaves(obj: Any, prefix: Tuple[str, ...] = ()):
    """(path, value) for each leaf of an overlay: non-dicts, None and empty dicts."""
    if isinstance(obj, dict) and obj:
        for key, value in obj.items():
            yield from _leaves(value, prefix + (key,))
    else:
        yield prefix, obj


def _drop(obj: dict, path: Tuple[str, ...]) -> None:
    """Delete `path` from nested dicts in place, removing parents it leaves empty."""
    if not path:
        return
    trail = []
    node: Any = obj
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        trail.append((node, key))
        node = node[key]
    if isinstance(node, dict):
        node.pop(path[-1], None)
    for parent, key in reversed(trail):
        if isinstance(parent[key], dict) and not parent[key]:
            del parent[key]
        else:
            break


def adopt(ctx: Context, save: bool = True) -> dict:
    """Make this host's overlay reproduce the current settings.json exactly.

    Returns the secret values kept local. With save=False only ctx.hostfile changes (for --dry-run).
    """
    current = read_current(ctx) or {}
    shared, secret = split_secrets(strip_tool_hook(current))
    if save:
        if secret:
            jsonutil.save(local_path(ctx), secret, mode=0o600)
        else:
            local_path(ctx).unlink(missing_ok=True)
    _set_overlay(ctx, jsonutil.unsubstitute(shared, ctx.placeholders()), save=save)
    return secret


def write(ctx: Context, backups: Optional[Backups], dry_run: bool = False, local: Optional[dict] = None) -> List[str]:
    """Write the generated settings.json if it differs. Returns the dotted paths that change."""
    gen = generate(ctx, local)
    current = read_current(ctx)
    changed: List[str] = []
    if current is None or not jsonutil.same(current, gen):
        changed = jsonutil.leaf_paths(jsonutil.diff(current or {}, gen)) or ["(new file)"]
        if not dry_run:
            if current is not None and backups is not None and not last_path(ctx).exists():
                backups.copy(settings_path(ctx))  # first write over a file agent-sync didn't make
            jsonutil.write_atomic(settings_path(ctx), jsonutil.dumps(gen), None if current is not None else 0o600)
    if not dry_run:
        jsonutil.save(last_path(ctx), gen, mode=0o600)
    return changed


def factor(ctx: Context, hosts: List[str]) -> dict:
    """Move settings that are identical on every host into base.json. Each host's result is unchanged."""
    base = _load(base_path(ctx))
    files = {h: _load(ctx.repo / "hosts" / f"{h}.json") for h in hosts}
    effective_by_host = {h: jsonutil.merge(base, f.get("settings", {}) or {}) for h, f in files.items()}
    new_base = jsonutil.common(list(effective_by_host.values()))
    jsonutil.save(base_path(ctx), new_base)
    for h, f in files.items():
        overlay = jsonutil.diff(new_base, effective_by_host[h])
        if overlay:
            f["settings"] = overlay
        else:
            f.pop("settings", None)
        jsonutil.save(ctx.repo / "hosts" / f"{h}.json", f)
    return new_base


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
