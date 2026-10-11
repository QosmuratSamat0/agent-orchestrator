from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
import tomllib


@dataclass
class BridgeConfig:
    command: str = "agent-bridge"
    wait_chunk_sec: float = 60.0
    stall_timeout_sec: float = 1800.0


@dataclass
class CodexConfig:
    command: str = r"C:\Users\admin\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe"
    exec_timeout_sec: float = 300.0
    sandbox_mode: str = "workspace-write"
    skip_git_repo_check: bool = True
    ephemeral: bool = True


@dataclass
class AntigravityConfig:
    command: str = r"C:\Users\admin\AppData\Local\agy\bin\agy.exe"
    default_effort: str = "medium"


@dataclass
class ModelRoutingConfig:
    trivial: str = "gemini-3.8-flash-low"
    simple: str = "gemini-3.8-flash-low"
    moderate: str = "gemini-3.8-flash-high"
    complex: str = "gemini-3.1-pro-high"
    expert: str = "gemini-3.1-pro-high"
    fallback: str = "gemini-3.8-flash-high"
    default_effort: str = "medium"


@dataclass
class ProgressConfig:
    enabled: bool = True
    poll_interval_sec: float = 10.0
    log_dir: str = ""


@dataclass
class OrchestratorConfig:
    db_path: str = "orchestrator.sqlite"
    max_active_tasks: int = 1
    total_task_timeout_sec: float = 2700.0
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    antigravity: AntigravityConfig = field(default_factory=AntigravityConfig)
    models: ModelRoutingConfig = field(default_factory=ModelRoutingConfig)
    progress: ProgressConfig = field(default_factory=ProgressConfig)

    @classmethod
    def load(cls, config_path: Optional[str | Path] = None) -> OrchestratorConfig:
        config = cls()
        path = (
            Path(config_path)
            if config_path
            else Path(os.environ.get("ORCHESTRATOR_CONFIG", "config.toml"))
        )

        if not path.is_file():
            # Check default config.example.toml or return defaults
            example_path = Path("config.example.toml")
            if example_path.is_file() and not path.exists():
                path = example_path
            else:
                return config

        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            return config

        if "orchestrator" in data:
            orch = data["orchestrator"]
            if "db_path" in orch:
                config.db_path = str(orch["db_path"])
            if "max_active_tasks" in orch:
                config.max_active_tasks = int(orch["max_active_tasks"])
            if "total_task_timeout_sec" in orch:
                config.total_task_timeout_sec = float(orch["total_task_timeout_sec"])

        if "bridge" in data:
            br = data["bridge"]
            if "command" in br:
                config.bridge.command = str(br["command"])
            if "wait_chunk_sec" in br:
                config.bridge.wait_chunk_sec = float(br["wait_chunk_sec"])
            if "stall_timeout_sec" in br:
                config.bridge.stall_timeout_sec = float(br["stall_timeout_sec"])

        if "codex" in data:
            cx = data["codex"]
            if "command" in cx:
                config.codex.command = str(cx["command"])
            if "exec_timeout_sec" in cx:
                config.codex.exec_timeout_sec = float(cx["exec_timeout_sec"])
            if "sandbox_mode" in cx:
                config.codex.sandbox_mode = str(cx["sandbox_mode"])

        if "antigravity" in data:
            ag = data["antigravity"]
            if "command" in ag:
                config.antigravity.command = str(ag["command"])
            if "default_effort" in ag:
                config.antigravity.default_effort = str(ag["default_effort"])

        if "models" in data:
            m = data["models"]
            if "trivial" in m:
                config.models.trivial = str(m["trivial"])
            if "simple" in m:
                config.models.simple = str(m["simple"])
            if "moderate" in m:
                config.models.moderate = str(m["moderate"])
            if "complex" in m:
                config.models.complex = str(m["complex"])
            if "expert" in m:
                config.models.expert = str(m["expert"])
            if "fallback" in m:
                config.models.fallback = str(m["fallback"])

        if "progress" in data:
            progress = data["progress"]
            if "enabled" in progress:
                config.progress.enabled = bool(progress["enabled"])
            if "poll_interval_sec" in progress:
                config.progress.poll_interval_sec = float(progress["poll_interval_sec"])
            if "log_dir" in progress:
                config.progress.log_dir = str(progress["log_dir"])

        return config
