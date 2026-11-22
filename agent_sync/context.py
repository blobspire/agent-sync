"""Where things live, and the three config files agent-sync reads.

- Tool config, per machine: ~/.config/agent-sync/config.json (repo path, this machine's host name, git identity).
- Repo manifest, shared:    <repo>/agent-sync.json (format, ignore list, host-only entries, notify command).
- Host file, per machine:   <repo>/hosts/<host>.json (settings overlay, excluded entries).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import FORMAT, jsonutil

KINDS = ("skills", "commands", "agents")
REPO_LINK_NAME = "agent-config"  # ~/.claude/agent-config -> the config repo checkout


class AgentSyncError(Exception):
    """An error with a message meant for the user; the CLI prints it without a traceback."""


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


@dataclass
class Paths:
    home: Path
    claude: Path
    config_dir: Path
    state_dir: Path
    default_repo: Path

    @classmethod
    def from_env(cls) -> "Paths":
        home = Path.home()
        claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        return cls(
            home=home,
            claude=claude,
            config_dir=_xdg("XDG_CONFIG_HOME", ".config") / "agent-sync",
            state_dir=_xdg("XDG_STATE_HOME", ".local/state") / "agent-sync",
            default_repo=_xdg("XDG_DATA_HOME", ".local/share") / "agent-config",
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def repo_link(self) -> Path:
        return self.claude / REPO_LINK_NAME

    def tilde(self, path: Path) -> str:
        """Show a path with ~ for the home directory."""
        try:
            return "~/" + str(Path(path).relative_to(self.home))
        except ValueError:
            return str(path)


def default_command() -> str:
    """Absolute path of the agent-sync command, for timers and hooks to call."""
    found = shutil.which("agent-sync")
    if found:
        return found
    return str(Path(sys.argv[0]).resolve())


@dataclass
class Context:
    paths: Paths
    repo: Path
    host: str
    git_name: str
    git_email: str
    command: str
    manifest: dict = field(default_factory=dict)
    hostfile: dict = field(default_factory=dict)

    # ---- loading --------------------------------------------------------------------------------

    @classmethod
    def load(cls, paths: Optional[Paths] = None) -> "Context":
        paths = paths or Paths.from_env()
        try:
            cfg = jsonutil.load(paths.config_file, None)
        except ValueError as exc:
            raise AgentSyncError(f"{paths.config_file} is not valid JSON: {exc}")
        if not cfg:
            raise AgentSyncError(
                f"agent-sync isn't set up on this computer ({paths.tilde(paths.config_file)} is missing).\n"
                "Run `agent-sync init <config-repo-url> --host <name>` (or `agent-sync new --host <name>`)."
            )
        ctx = cls(
            paths=paths,
            repo=Path(cfg["repo"]),
            host=cfg["host"],
            git_name=cfg.get("git", {}).get("name", ""),
            git_email=cfg.get("git", {}).get("email", ""),
            command=cfg.get("command") or default_command(),
        )
        ctx.reload()
        return ctx

    def reload(self) -> None:
        """Re-read the repo's manifest and host file (they change when a pull lands)."""
        if not (self.repo / ".git").exists():
            raise AgentSyncError(f"The config repo {self.repo} is missing or isn't a git checkout.")
        try:
            self.manifest = jsonutil.load(self.repo / "agent-sync.json", {}) or {}
            self.hostfile = jsonutil.load(self.hostfile_path, {}) or {}
        except ValueError as exc:
            raise AgentSyncError(f"A config file in {self.repo} is not valid JSON: {exc}")
        fmt = self.manifest.get("format", 1)
        if not isinstance(fmt, int) or fmt > FORMAT:
            raise AgentSyncError(
                f"The config repo uses format {fmt}, but this agent-sync only understands format {FORMAT}. "
                "Update agent-sync (git pull in its checkout, then ./install.sh) and try again."
            )

    def save_config(self) -> None:
        jsonutil.save(
            self.paths.config_file,
            {
                "repo": str(self.repo),
                "host": self.host,
                "git": {"name": self.git_name, "email": self.git_email},
                "command": self.command,
            },
        )

    # ---- repo files -----------------------------------------------------------------------------

    @property
    def hostfile_path(self) -> Path:
        return self.repo / "hosts" / f"{self.host}.json"

    @property
    def host_md(self) -> Path:
        return self.repo / "hosts" / f"{self.host}.md"

    def save_hostfile(self) -> None:
        jsonutil.save(self.hostfile_path, self.hostfile)

    def ignored(self, kind: str) -> List[str]:
        names = list(self.manifest.get("ignore", []))
        if kind == "skills":
            names.append("synced")  # claude.ai's own skill sync owns ~/.claude/skills/synced
        return names

    def allowed(self, kind: str, name: str) -> bool:
        """Whether a repo entry (kind/name) should be installed on this host."""
        only = self.manifest.get("only", {}).get(f"{kind}/{name}")
        if only is not None and self.host not in only:
            return False
        return name not in self.hostfile.get("exclude", {}).get(kind, [])

    def placeholders(self) -> jsonutil.Mapping:
        return [
            ("{{AGENT_SYNC}}", self.command),
            ("{{CONFIG}}", str(self.repo)),
            ("{{HOME}}", str(self.paths.home)),
        ]


def validate_host(name: str) -> str:
    ok = name and all(c.isalnum() or c in "-_." for c in name) and not name.startswith(".")
    if not ok:
        raise AgentSyncError(f"Host name {name!r} must be letters, digits, '-', '_' or '.'.")
    return name
