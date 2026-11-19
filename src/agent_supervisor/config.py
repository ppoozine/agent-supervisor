"""全域設定：各種上限與權限政策。從 TOML 檔讀取，沒有檔案時使用預設值。

數值的預設值都是暫定的，見 `docs/03-decisions.md` 的「尚未決定」。
"""

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from agent_supervisor.adapters.base import PermissionPolicy, SandboxLevel

DEFAULT_DATA_DIR = Path.home() / ".agent-supervisor"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.toml"

DEFAULT_TOOL_RULES: dict[str, Any] = {
    # Agent 可以使用的工具；清單外的一律拒絕。
    "allowed_tools": ["Read", "Glob", "Grep", "Write", "Edit", "MultiEdit", "Bash", "TodoWrite"],
    # 以這些字串開頭的指令一律拒絕。push 與開 PR 由 supervisor 執行。
    "denied_commands": ["git push", "gh", "sudo", "rm -rf /"],
}


class ConfigError(ValueError):
    """設定檔內容不合法。"""


@dataclass(frozen=True)
class Config:
    """執行期設定。"""

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
        """SQLite 資料庫檔案的位置。"""
        return self.data_dir / "agent-supervisor.db"

    @property
    def worktrees_dir(self) -> Path:
        """各任務 worktree 的上層目錄。"""
        return self.data_dir / "worktrees"

    @property
    def lock_path(self) -> Path:
        """確保同時只有一個 daemon 的鎖檔位置。"""
        return self.data_dir / "daemon.lock"


def absolute_path(value: str) -> Path:
    """把使用者給的路徑變成絕對路徑（展開 `~`、解開符號連結）。CLI 與 API 共用。"""
    return Path(value).expanduser().resolve()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """讀取設定檔；檔案不存在時回傳預設值。"""
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
