"""Global settings: limits and the permission policy. Read from TOML; defaults if no file.

All numeric defaults are provisional; see the "not yet decided" section of
`docs/design/decisions.md`.
"""

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from agent_supervisor.adapters.base import PermissionPolicy, SandboxLevel

DEFAULT_DATA_DIR = Path.home() / ".agent-supervisor"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.toml"

DEFAULT_TOOL_RULES: dict[str, Any] = {
    # Tools the agent may use; anything not on the list is always denied.
    "allowed_tools": ["Read", "Glob", "Grep", "Write", "Edit", "MultiEdit", "Bash", "TodoWrite"],
    # Commands starting with these strings are always denied. The supervisor does the push and opens
    # the PR.
    "denied_commands": ["git push", "gh", "sudo", "rm -rf /"],
}


class ConfigError(ValueError):
    """The config file is invalid."""


@dataclass(frozen=True)
class Config:
    """Runtime settings."""

    data_dir: Path = DEFAULT_DATA_DIR
    task_cost_limit_usd: float = 5.0
    task_token_limit: int = 20_000_000
    max_turns_per_run: int = 50
    run_timeout_seconds: float = 1800.0
    poll_interval_seconds: float = 30.0
    api_port: int = 8765
    ci_log_max_chars: int = 20_000
    notify_needs_you: bool = True
    permission: PermissionPolicy = field(
        default_factory=lambda: PermissionPolicy(tool_rules=DEFAULT_TOOL_RULES)
    )

    @property
    def db_path(self) -> Path:
        """Location of the SQLite database file."""
        return self.data_dir / "agent-supervisor.db"

    @property
    def worktrees_dir(self) -> Path:
        """Parent directory of each task's worktree."""
        return self.data_dir / "worktrees"

    @property
    def lock_path(self) -> Path:
        """Location of the lock file that ensures only one daemon runs at a time."""
        return self.data_dir / "daemon.lock"


def absolute_path(value: str) -> Path:
    """Make a user-given path absolute (expand `~`, resolve symlinks). Shared by CLI and API."""
    return Path(value).expanduser().resolve()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Read the config file; return the defaults if it does not exist."""
    if not path.exists():
        return Config()
    with path.open("rb") as file:
        raw = tomllib.load(file)

    permission_raw = raw.pop("permission", {})
    known = {item.name for item in fields(Config)} - {"permission"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    if "data_dir" in raw:
        raw["data_dir"] = Path(raw["data_dir"]).expanduser()
    return replace(Config(), permission=_load_permission(permission_raw), **raw)


def _load_permission(raw: dict[str, Any]) -> PermissionPolicy:
    default = Config().permission
    unknown = set(raw) - {"sandbox_level", "allow_network", "tool_rules"}
    if unknown:
        raise ConfigError(f"unknown permission keys: {', '.join(sorted(unknown))}")
    try:
        sandbox_level = SandboxLevel(raw.get("sandbox_level", default.sandbox_level))
    except ValueError as error:
        raise ConfigError(f"invalid sandbox_level: {raw['sandbox_level']!r}") from error
    return PermissionPolicy(
        sandbox_level=sandbox_level,
        allow_network=bool(raw.get("allow_network", default.allow_network)),
        tool_rules=raw.get("tool_rules", default.tool_rules),
    )
