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
    pass


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
        raise Unreadable(str(exc))
    if data is not None and not isinstance(data, dict):
        raise Unreadable("top level is not an object")
    return data


def _load(path: Path) -> dict:
    try:
        data = jsonutil.load(path, {}) or {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---- agent-sync's own SessionStart hook ---------------------------------------------------------


def _is_tool_hook(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks", []) or []:
        cmd = hook.get("command", "") if isinstance(hook, dict) else ""
        if "agent-sync" in cmd and HOOK_MARK in cmd:
            return True
    return False


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
        kept = [e for e in hooks["SessionStart"] if not _is_tool_hook(e)]
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
    return jsonutil.merge(_load(base_path(ctx)), ctx.hostfile.get("settings", {}) or {})


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


def _add_local(ctx: Context, secrets: dict) -> None:
    if secrets:
        jsonutil.save(local_path(ctx), jsonutil.merge(_load(local_path(ctx)), secrets), mode=0o600)


def capture(ctx: Context, dry_run: bool = False) -> List[str]:
    """Fold changes made to settings.json since agent-sync last wrote it into this host's overlay.

    Returns the dotted paths that changed. Raises Unreadable if settings.json is mid-edit or broken.
    """
    current = read_current(ctx)
    last = jsonutil.load(last_path(ctx), None) if last_path(ctx).exists() else None
    if current is None or not isinstance(last, dict):
        return []
    cur, prev = strip_tool_hook(current), strip_tool_hook(last)
    if jsonutil.same(cur, prev):
        return []
    delta = jsonutil.diff(prev, cur)
    if dry_run:
        return jsonutil.leaf_paths(delta)
    shared, secret = split_secrets(delta)
    _add_local(ctx, secret)
    if shared:
        placeholder_delta = jsonutil.unsubstitute(shared, ctx.placeholders())
        _set_overlay(ctx, jsonutil.merge(effective(ctx), placeholder_delta))
    return jsonutil.leaf_paths(delta)


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
