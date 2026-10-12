"""Detached task owner used by the MCP server."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import TaskStatus
from agent_orchestrator.orchestrator import Orchestrator


logger = logging.getLogger(__name__)


def run_existing(
    task_id: str, config_path: str | None, db_path: str, *, recover: bool = False,
    orchestrator_factory=Orchestrator,
) -> TaskStatus:
    if not Path(db_path).is_absolute():
        raise ValueError("Путь к базе данных worker должен быть абсолютным.")
    config = OrchestratorConfig.load(config_path)
    config.db_path = db_path
    orchestrator = orchestrator_factory(config=config)
    try:
        if orchestrator.repo.get_task(task_id) is None:
            raise ValueError("Задача для worker не найдена.")
        if recover:
            orchestrator.recover(task_id=task_id, allow_dispatch=False)
            return orchestrator.repo.get_task(task_id).status
        return orchestrator.run_task(task_id).status
    except Exception as exc:
        logger.exception("Сбой worker для задачи %s", task_id)
        task = orchestrator.repo.get_task(task_id)
        if task and not task.status.is_terminal:
            task.mark_failed(f"Сбой оркестратора: {exc}")
            orchestrator.repo.update_task(task)
            orchestrator.repo.record_event(task_id, "TASK_FAILED", {"error": str(exc)})
            orchestrator._finish(task)
            return task.status
        raise
    finally:
        orchestrator.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Внутренний worker Agent Orchestrator")
    parser.add_argument("task_id")
    parser.add_argument("--config")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    status = run_existing(args.task_id, args.config, args.db_path, recover=args.recover)
    raise SystemExit(0 if status == TaskStatus.COMPLETED else 1)


if __name__ == "__main__":
    main()
