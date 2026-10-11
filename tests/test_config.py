from __future__ import annotations

from pathlib import Path
import pytest
from agent_orchestrator.config import OrchestratorConfig


def test_config_defaults():
    cfg = OrchestratorConfig()
    assert cfg.total_task_timeout_sec == 2700.0
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
