"""config.py 的測試：預設值與 TOML 設定檔。"""

from pathlib import Path

import pytest

from agent_supervisor.adapters.base import SandboxLevel
from agent_supervisor.config import Config, ConfigError, load_config


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config == Config()
    assert config.db_path == config.data_dir / "agent-supervisor.db"
    assert config.lock_path == config.data_dir / "daemon.lock"


def test_values_from_file_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
data_dir = "/tmp/asv-data"
task_cost_limit_usd = 1.5
task_token_limit = 1000
max_turns_per_run = 3
run_timeout_seconds = 60

[permission]
sandbox_level = "read-only"
allow_network = false

[permission.tool_rules]
allowed_tools = ["Read"]
"""
    )

    config = load_config(path)

    assert config.data_dir == Path("/tmp/asv-data")
    assert config.task_cost_limit_usd == 1.5
    assert config.task_token_limit == 1000
    assert config.max_turns_per_run == 3
    assert config.run_timeout_seconds == 60
    assert config.permission.sandbox_level == SandboxLevel.READ_ONLY
    assert config.permission.allow_network is False
    assert config.permission.tool_rules == {"allowed_tools": ["Read"]}


def test_default_policy_does_not_allow_push_or_gh() -> None:
    denied = Config().permission.tool_rules["denied_commands"]

    assert "git push" in denied
    assert "gh" in denied


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("task_cost_limit = 3\n")

    with pytest.raises(ConfigError, match="task_cost_limit"):
        load_config(path)


def test_invalid_sandbox_level_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[permission]\nsandbox_level = "anything-goes"\n')

    with pytest.raises(ConfigError, match="sandbox_level"):
        load_config(path)
