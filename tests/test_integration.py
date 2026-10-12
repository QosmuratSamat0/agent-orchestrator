from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
import pytest

from agent_orchestrator.cli import main
from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import TaskStatus, VerificationVerdict
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.repository import TaskRepository
from agent_orchestrator.models import Task
from tests.conftest import MockBridgeClient, MockCodexVerifier


@pytest.mark.parametrize("status, exit_code", [
    (TaskStatus.FAILED, 1),
    (TaskStatus.COMPLETED, 0),
])
def test_cmd_run_exit_code_and_close_with_fake_orchestrator(
    tmp_path, monkeypatch, capsys, status, exit_code
):
    from agent_orchestrator import cli

    task = Task(title="Проверка", prompt="Задача", cwd=str(tmp_path))
    if status == TaskStatus.FAILED:
        task.mark_failed("Ошибка Bridge")
    else:
        task.mark_completed()
    report_path = tmp_path / f"orchestrator_report_{task.id[:8]}.md"
    report_path.write_text("Отчёт", encoding="utf-8")
    closed = []

    class FakeOrchestrator:
        def __init__(self, config):
            self.progress = None

        def create_task(self, **_):
            return task

        def run_task(self, *_args, **_kwargs):
            return task

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli.OrchestratorConfig, "load", lambda *_: OrchestratorConfig())
    args = argparse.Namespace(config=None, title=task.title, prompt=task.prompt,
                              criteria="", cwd=str(tmp_path), model=None, effort=None)
    with pytest.raises(SystemExit) as result:
        cli.cmd_run(args)
    assert result.value.code == exit_code
    assert closed == [True]
    output = capsys.readouterr().out
    assert status.value.upper() in output
    assert str(report_path) in output


def test_cli_models_command(capsys):
    test_args = ["agent-orchestrator", "models"]
    orig_argv = sys.argv
    try:
        sys.argv = test_args
        main()
        captured = capsys.readouterr()
        assert "ПОДТВЕРЖДЁННЫЕ МОДЕЛИ ANTIGRAVITY" in captured.out
        assert "gemini-3.8-flash-high" in captured.out
        assert "gemini-3.1-pro-high" in captured.out
    finally:
        sys.argv = orig_argv


def test_cli_list_and_status(tmp_path: Path, capsys, monkeypatch):
    db_file = tmp_path / "cli_test.sqlite"
    cfg = OrchestratorConfig(db_path=str(db_file))
    orchestrator = Orchestrator(config=cfg)

    # Submit task
    t = orchestrator.create_task(
        title="CLI Test Task", prompt="Test prompt", cwd=str(tmp_path)
    )

    test_args = ["agent-orchestrator", "-c", str(tmp_path / "dummy.toml"), "list"]
    orig_argv = sys.argv
    try:
        sys.argv = ["agent-orchestrator", "status", t.id]
        # Monkeypatch config in cmd
        from agent_orchestrator import cli
        monkeypatch.setattr(cli.OrchestratorConfig, "load", lambda *a, **k: cfg)
        cli.main()
        captured = capsys.readouterr()
        assert "CLI Test Task" in captured.out
        assert t.id in captured.out
    finally:
        sys.argv = orig_argv


def test_read_and_cancel_cli_do_not_start_router_or_subprocess(tmp_path, monkeypatch, capsys):
    from agent_orchestrator import cli, orchestrator as module

    cfg = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    repo = TaskRepository(cfg.db_path)
    task = repo.create_task(Task(title="Задача", prompt="Проверить", cwd=str(tmp_path)))
    monkeypatch.setattr(cli.OrchestratorConfig, "load", lambda *_, **__: cfg)
    def forbidden(*_, **__):
        raise AssertionError("Запуск процесса или маршрутизатора запрещён")
    monkeypatch.setattr(module.ModelRouter, "__init__", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    for handler, task_id in ((cli.cmd_status, task.id), (cli.cmd_list, None),
                             (cli.cmd_report, task.id), (cli.cmd_cancel, task.id)):
        handler(__import__("argparse").Namespace(config=None, task_id=task_id, status=None))
    output = capsys.readouterr().out
    assert "Задача" in output
    assert "Отмена записана в базе данных" in output
    assert repo.get_task(task.id).status == TaskStatus.CANCELLED


def test_real_sqlite_file_persistence_and_reload(tmp_path: Path):
    db_file = tmp_path / "persistence_test.sqlite"
    cfg = OrchestratorConfig(db_path=str(db_file))

    # Phase 1: create orchestrator and write task
    orch1 = Orchestrator(config=cfg)
    task = orch1.create_task(
        title="Persisted Task", prompt="Ensure survives reload", cwd=str(tmp_path)
    )
    task_id = task.id
    task.mark_running("bridge_id_101")
    orch1.repo.update_task(task)

    # Phase 2: simulate new process, reload from SQLite file
    orch2 = Orchestrator(config=cfg)
    reloaded = orch2.repo.get_task(task_id)
    assert reloaded is not None
    assert reloaded.id == task_id
    assert reloaded.title == "Persisted Task"
    assert reloaded.status == TaskStatus.RUNNING
    assert reloaded.bridge_task_id == "bridge_id_101"


def test_cli_cancel_of_finished_task_says_nothing_to_cancel(tmp_path, monkeypatch, capsys):
    # Seen in a real run: cancelling an already completed task printed "Отмена записана...".
    from agent_orchestrator import cli

    cfg = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    repo = TaskRepository(cfg.db_path)
    task = repo.create_task(Task(title="Готово", prompt="p", cwd=str(tmp_path)))
    task.mark_completed()
    repo.update_task(task)
    monkeypatch.setattr(cli.OrchestratorConfig, "load", lambda *_, **__: cfg)

    cli.cmd_cancel(argparse.Namespace(config=None, task_id=task.id))

    output = capsys.readouterr().out
    assert "уже завершена" in output and "completed" in output
    assert "Отмена записана" not in output
    assert repo.get_task(task.id).status == TaskStatus.COMPLETED
