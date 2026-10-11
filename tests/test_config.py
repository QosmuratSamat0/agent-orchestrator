from __future__ import annotations

from pathlib import Path
import pytest
from agent_orchestrator.config import OrchestratorConfig


def test_config_defaults():
    cfg = OrchestratorConfig()
    assert cfg.total_task_timeout_sec == 2700.0
    assert cfg.progress.enabled is True
    assert cfg.progress.poll_interval_sec == 10.0
    assert cfg.progress.log_dir == ""
    assert not hasattr(cfg, "max_retries")


def test_config_loads_toml_with_legacy_max_retries(tmp_path: Path):
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        """
[orchestrator]
db_path = "custom.sqlite"
max_retries = 3
max_active_tasks = 2
total_task_timeout_sec = 1800.0
""",
        encoding="utf-8",
    )

    loaded = OrchestratorConfig.load(str(toml_file))
    assert loaded.db_path == "custom.sqlite"
    assert loaded.max_active_tasks == 2
    assert loaded.total_task_timeout_sec == 1800.0
    assert not hasattr(loaded, "max_retries")


def test_progress_config_loads_from_toml(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[progress]\nenabled = false\npoll_interval_sec = 3.5\nlog_dir = "logs"\n', encoding="utf-8")
    config = OrchestratorConfig.load(path)
    assert config.progress.enabled is False
    assert config.progress.poll_interval_sec == 3.5
    assert config.progress.log_dir == "logs"
