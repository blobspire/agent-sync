"""Run `agent-sync --quiet` every 5 minutes: a launchd agent on macOS, a systemd user timer on Linux."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple
from xml.sax.saxutils import escape

from .context import AgentSyncError, Context

INTERVAL_SECONDS = 300
LABEL = "dev.agent-sync"
UNIT = "agent-sync"


def search_path() -> str:
    """PATH for the timer: wherever git, gh and python3 live now, plus the usual system dirs."""
    dirs: List[str] = []
    for tool in ("git", "gh", "python3", "agent-sync"):
        found = shutil.which(tool)
        if found:
            dirs.append(os.path.dirname(found))
    dirs += [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return ":".join(out)


def launchd_plist(command: str, path_env: str, log_dir: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(command)}</string>
    <string>--quiet</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{escape(path_env)}</string>
  </dict>
  <key>StartInterval</key><integer>{INTERVAL_SECONDS}</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>{escape(str(log_dir / "timer.out"))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log_dir / "timer.out"))}</string>
</dict>
</plist>
"""


def systemd_units(command: str, path_env: str) -> Tuple[str, str]:
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    service = f"""[Unit]
Description=agent-sync: sync the shared coding-agent setup

[Service]
Type=oneshot
Environment={q("PATH=" + path_env)}
ExecStart={q(command)} --quiet
Nice=10
"""
    timer = f"""[Unit]
Description=Run agent-sync every {INTERVAL_SECONDS // 60} minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec={INTERVAL_SECONDS // 60}min

[Install]
WantedBy=timers.target
"""
    return service, timer


def _plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AgentSyncError(f"{' '.join(cmd)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def install(ctx: Context) -> str:
    path_env = search_path()
    if platform.system() == "Darwin":
        plist = _plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        ctx.paths.state_dir.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(ctx.command, path_env, ctx.paths.state_dir))
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", domain, str(plist)], check=False)
        _run(["launchctl", "bootstrap", domain, str(plist)])
        return f"launchd agent {LABEL} installed ({plist}); runs every {INTERVAL_SECONDS // 60} minutes"
    if not shutil.which("systemctl"):
        raise AgentSyncError("No launchd or systemd here. Run `agent-sync --quiet` every 5 minutes from cron instead.")
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service, timer = systemd_units(ctx.command, path_env)
    (unit_dir / f"{UNIT}.service").write_text(service)
    (unit_dir / f"{UNIT}.timer").write_text(timer)
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
    note = ""
    linger = _run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"], check=False).stdout
    if "Linger=no" in linger:
        note = " Note: lingering is off, so it only runs while you're logged in (`loginctl enable-linger` fixes that)."
    return f"systemd user timer {UNIT}.timer enabled; runs every {INTERVAL_SECONDS // 60} minutes.{note}"


def remove() -> str:
    if platform.system() == "Darwin":
        plist = _plist_path()
        if plist.exists():
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False)
            plist.unlink()
            return f"removed launchd agent {LABEL}"
        return "no launchd agent installed"
    unit_dir = _unit_dir()
    if (unit_dir / f"{UNIT}.timer").exists():
        _run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
        for suffix in ("timer", "service"):
            (unit_dir / f"{UNIT}.{suffix}").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        return f"removed systemd timer {UNIT}.timer"
    return "no systemd timer installed"


def status() -> Tuple[bool, str]:
    if platform.system() == "Darwin":
        if not _plist_path().exists():
            return False, "not installed"
        r = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False)
        return (r.returncode == 0, "loaded" if r.returncode == 0 else "installed but not loaded")
    if not (_unit_dir() / f"{UNIT}.timer").exists():
        return False, "not installed"
    r = _run(["systemctl", "--user", "is-active", f"{UNIT}.timer"], check=False)
    state = r.stdout.strip() or "unknown"
    return state == "active", state
