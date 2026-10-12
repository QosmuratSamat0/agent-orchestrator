from __future__ import annotations

import argparse
import re
import subprocess
import pytest

from agent_orchestrator.bridge_client import BridgeClient
from agent_orchestrator.cli import cmd_watch
from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import Task, TaskStatus, VerificationVerdict
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.progress import ProgressFeed
from agent_orchestrator.repository import TaskRepository
from tests.conftest import MockBridgeClient, MockCodexVerifier


def test_progress_feed_throttles_and_tails_without_duplicates(tmp_path):
    ticks = [0.0]
    task = Task(id="12345678-test", bridge_session_id="session", retry_count=1)
    bridge = MockBridgeClient()
    bridge.transcript_events["session"] = [{"type": "message", "data": {"text": "Первое событие"}}]
    feed = ProgressFeed(tmp_path / "logs", poll_interval_sec=10, clock=lambda: ticks[0])
    log_path = tmp_path / "logs" / "12345678.log"
    assert not log_path.exists()

    resp = {"status": "running", "silent_for_sec": 2, "recent_activity": ["Работа идёт"]}
    feed.on_wait_progress(task, bridge, resp, deadline_epoch=100, now=10)
    first = log_path.read_text(encoding="utf-8")
    assert "статус running" in first and "тишина 2 с" in first
    assert "осталось 01:30" in first
    assert "Первое событие" in first
    assert "активность: Работа идёт" in first

    ticks[0] = 5
    feed.on_wait_progress(task, bridge, resp, deadline_epoch=100, now=15)
    assert log_path.read_text(encoding="utf-8") == first
    ticks[0] = 11
    feed.on_wait_progress(task, bridge, resp, deadline_epoch=100, now=21)
    updated = log_path.read_text(encoding="utf-8")
    assert updated.count("статус running") == 2
    assert updated.count("Первое событие") == 1
    assert updated.count("активность: Работа идёт") == 1
    assert feed._offsets["session"] == 1


def test_progress_feed_status_change_and_three_page_limit(tmp_path):
    ticks = [0.0]
    calls = []
    task = Task(id="12345678-test", bridge_session_id="session")

    class ManyPages:
        def get_transcript(self, session_id, offset=0, limit=50):
            calls.append(offset)
            return {"ok": True, "events": [{"type": "message", "data": {"text": f"страница {offset}"}}],
                    "next_offset": offset + 1}

    feed = ProgressFeed(tmp_path / "logs", poll_interval_sec=10, clock=lambda: ticks[0])
    feed.on_wait_progress(task, ManyPages(), {"status": "running"})
    assert calls == [0, 1, 2]
    ticks[0] = 1
    feed.on_wait_progress(task, ManyPages(), {"status": "completed"})
    assert calls == [0, 1, 2, 3, 4, 5]
    assert feed._offsets["session"] == 6


def test_progress_feed_truncates_multiline_and_handles_errors(tmp_path):
    task = Task(id="12345678-test", bridge_session_id="session")
    feed = ProgressFeed(tmp_path / "logs", clock=lambda: 0, max_line_chars=25)

    class LongThenBroken:
        def __init__(self):
            self.calls = 0

        def get_transcript(self, session_id, offset=0, limit=50):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("Транскрипт недоступен")
            return {"ok": True, "events": [{"type": "message", "data": {"text": "Длинная строка\n" + "я" * 100}}],
                    "next_offset": 1}

    bridge = LongThenBroken()
    feed.on_wait_progress(task, bridge, {})
    lines = (tmp_path / "logs" / "12345678.log").read_text(encoding="utf-8").splitlines()
    assert "статус неизвестен" in lines[0]
    assert len(lines) == 2
    assert len(lines[1].split(" | ", 1)[1]) <= 25
    assert "\n" not in lines[1]
    assert feed._offsets["session"] == 1


def test_progress_feed_disabled_is_lazy(tmp_path):
    task = Task(id="12345678-test")
    feed = ProgressFeed(tmp_path / "logs", enabled=False)
    feed.log(task, "Текст")
    feed.on_wait_progress(task, MockBridgeClient(), {})
    assert not (tmp_path / "logs").exists()


def test_bridge_get_transcript_builds_arguments(monkeypatch):
    calls = []
    bridge = BridgeClient(command="mock")
    monkeypatch.setattr(bridge, "call_tool", lambda name, args: calls.append((name, args)) or {"ok": True})
    assert bridge.get_transcript("session", offset=4, limit=7) == {"ok": True}
    bridge.get_transcript("session", kinds=["message"])
    assert calls == [
        ("get_transcript", {"session_id": "session", "offset": 4, "limit": 7}),
        ("get_transcript", {"session_id": "session", "offset": 0, "limit": 50, "kinds": ["message"]}),
    ]


def test_orchestrator_progress_logs_steps_without_events(tmp_path):
    class RunningBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def wait_task(self, task_id, timeout_sec=180.0):
            self.calls += 1
            state = "running" if self.calls <= 3 else "completed"
            return {"ok": True, "status": state, "recent_activity": ["Файл изменён"]}

    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    config.progress.poll_interval_sec = 0.1
    bridge = RunningBridge()
    verifier = MockCodexVerifier()
    orchestrator = Orchestrator(config=config, bridge=bridge, verifier=verifier)
    task = orchestrator.create_task("Прогресс", "Задача", cwd=str(tmp_path))
    bridge.transcript_events["mock_sess_1"] = [{"type": "message", "data": {"text": "Пишу код"}}]

    result = orchestrator.run_task(task.id)
    assert result.status == TaskStatus.COMPLETED
    assert bridge.calls == 4
    log = (tmp_path / "orchestrator_logs" / f"{task.id[:8]}.log").read_text(encoding="utf-8")
    for expected in ("статус running", "Пишу код", "Вердикт: passed", "Итоговый статус: completed"):
        assert expected in log
    assert [e.event_type for e in orchestrator.repo.get_events(task.id)] == [
        "TASK_CREATED", "TASK_DISPATCHED", "ANTIGRAVITY_FINISHED", "VERIFICATION_COMPLETED",
    ]


def test_transcript_failure_does_not_change_outcome(tmp_path):
    class BrokenTranscript(MockBridgeClient):
        def get_transcript(self, session_id, offset=0, limit=50, kinds=None):
            raise RuntimeError("Транскрипт недоступен")

    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    bridge = BrokenTranscript()
    orchestrator = Orchestrator(config=config, bridge=bridge, verifier=MockCodexVerifier())
    task = orchestrator.create_task("Проверка", "Задача", cwd=str(tmp_path))
    assert orchestrator.run_task(task.id).status == TaskStatus.COMPLETED
    assert [e.event_type for e in orchestrator.repo.get_events(task.id)] == [
        "TASK_CREATED", "TASK_DISPATCHED", "ANTIGRAVITY_FINISHED", "VERIFICATION_COMPLETED",
    ]


def test_progress_callback_failure_does_not_change_outcome(tmp_path):
    class BrokenProgress:
        enabled = True

        def log(self, task, message):
            raise RuntimeError("Лог недоступен")

        def on_wait_progress(self, task, bridge, resp, deadline_epoch=None):
            raise RuntimeError("Прогресс недоступен")

    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    orchestrator = Orchestrator(
        config=config, bridge=MockBridgeClient(), verifier=MockCodexVerifier(),
        progress=BrokenProgress(),
    )
    task = orchestrator.create_task("Проверка", "Задача", cwd=str(tmp_path))
    assert orchestrator.run_task(task.id).status == TaskStatus.COMPLETED
    assert [e.event_type for e in orchestrator.repo.get_events(task.id)] == [
        "TASK_CREATED", "TASK_DISPATCHED", "ANTIGRAVITY_FINISHED", "VERIFICATION_COMPLETED",
    ]


def test_progress_disabled_creates_no_log_directory(tmp_path):
    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    config.progress.enabled = False
    orchestrator = Orchestrator(config=config, bridge=MockBridgeClient(), verifier=MockCodexVerifier())
    task = orchestrator.create_task("Без лога", "Задача", cwd=str(tmp_path))
    assert orchestrator.run_task(task.id).status == TaskStatus.COMPLETED
    assert not (tmp_path / "orchestrator_logs").exists()


def test_external_cancel_wins_with_progress_enabled(tmp_path):
    db_path = tmp_path / "tasks.sqlite"
    config = OrchestratorConfig(db_path=str(db_path))

    class CancelOnWait(MockBridgeClient):
        def wait_task(self, task_id, timeout_sec=180.0):
            canceller.cancel_task(task.id)
            return {"ok": True, "status": "completed"}

    bridge = CancelOnWait()
    verifier = MockCodexVerifier()
    runner = Orchestrator(config=config, bridge=bridge, verifier=verifier)
    canceller = Orchestrator(config=config, repository=TaskRepository(db_path), bridge=bridge, verifier=verifier)
    task = runner.create_task("Отмена", "Задача", cwd=str(tmp_path))
    assert runner.run_task(task.id).status == TaskStatus.CANCELLED
    assert runner.repo.get_task(task.id).status == TaskStatus.CANCELLED
    assert verifier.verification_calls == []
    assert "Отменено пользователем." in (
        tmp_path / f"orchestrator_report_{task.id[:8]}.md"
    ).read_text(encoding="utf-8")


def test_watch_once_is_read_only_and_starts_no_processes(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "tasks.sqlite"
    repo = TaskRepository(db_path)
    task = repo.create_task(Task(title="Просмотр", prompt="Задача"))
    task.mark_running()
    task.retry_count = 1
    task.verification_verdict = VerificationVerdict.RETRY_NEEDED
    repo.update_task(task)
    repo.record_event(task.id, "TASK_DISPATCHED", {"attempt": 1})
    log_dir = tmp_path / "orchestrator_logs"
    log_dir.mkdir()
    (log_dir / f"{task.id[:8]}.log").write_text("[12:00:00] попытка 2 | Работа идёт\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[orchestrator]\ndb_path = "{db_path.as_posix()}"\n', encoding="utf-8")
    before = repo.get_task(task.id)
    event_count = len(repo.get_events(task.id))

    def forbidden(*args, **kwargs):
        raise AssertionError("Запуск процесса запрещён")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(BridgeClient, "__init__", forbidden)
    monkeypatch.setattr(Orchestrator, "__init__", forbidden)
    cmd_watch(argparse.Namespace(config=str(config_path), task_id=None, interval=2.0, once=True))
    output = capsys.readouterr().out
    assert "Статус: running" in output
    assert "Попытка: 2" in output
    assert re.search(r"Осталось: \d+:\d{2}", output)
    assert "TASK_CREATED" in output and "TASK_DISPATCHED" in output
    assert "Работа идёт" in output
    after = repo.get_task(task.id)
    assert (after.status, after.updated_at) == (before.status, before.updated_at)
    assert len(repo.get_events(task.id)) == event_count


def test_watch_empty_database(tmp_path, capsys):
    TaskRepository(tmp_path / "empty.sqlite")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[orchestrator]\ndb_path = "{(tmp_path / "empty.sqlite").as_posix()}"\n', encoding="utf-8")
    cmd_watch(argparse.Namespace(config=str(config_path), task_id=None, interval=2.0, once=True))
    assert "Задач нет." in capsys.readouterr().out


@pytest.mark.parametrize("missing_parent", [False, True])
def test_watch_missing_database_exits_without_creating_it(tmp_path, capsys, missing_parent):
    db_path = (tmp_path / "missing" if missing_parent else tmp_path) / "tasks.sqlite"
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[orchestrator]\ndb_path = "{db_path.as_posix()}"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        cmd_watch(argparse.Namespace(config=str(config_path), task_id=None, interval=2.0, once=True))
    assert error.value.code == 1
    assert f"База данных не найдена: {db_path.resolve()}" in capsys.readouterr().out
    assert not db_path.exists()


def test_watch_terminal_remaining_and_local_event_time(tmp_path, capsys):
    import sqlite3
    from datetime import datetime, timezone

    db_path = tmp_path / "tasks.sqlite"
    repo = TaskRepository(db_path)
    task = repo.create_task(Task(title="Завершено", prompt="Проверка", cwd=str(tmp_path)))
    task.mark_completed()
    repo.update_task(task)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?",
                     ("2024-01-02T03:04:05+00:00", task.id))
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[orchestrator]\ndb_path = "{db_path.as_posix()}"\n', encoding="utf-8")
    cmd_watch(argparse.Namespace(config=str(config_path), task_id=task.id, interval=2.0, once=True))
    output = capsys.readouterr().out
    expected = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S")
    assert "Осталось: —" in output
    assert expected in output


def test_watch_tails_only_new_lines_until_terminal(tmp_path, monkeypatch, capsys):
    from agent_orchestrator import cli

    db_path = tmp_path / "tasks.sqlite"
    repo = TaskRepository(db_path)
    task = repo.create_task(Task(title="Просмотр", prompt="Задача"))
    task.mark_running()
    repo.update_task(task)
    log_dir = tmp_path / "orchestrator_logs"
    log_dir.mkdir()
    log_path = log_dir / f"{task.id[:8]}.log"
    log_path.write_text("Первая строка\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[orchestrator]\ndb_path = "{db_path.as_posix()}"\n', encoding="utf-8")

    def finish(_interval):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("Вторая строка\n")
        current = repo.get_task(task.id)
        current.mark_completed()
        repo.update_task(current)

    monkeypatch.setattr(cli.time, "sleep", finish)
    cmd_watch(argparse.Namespace(config=str(config_path), task_id=None, interval=2.0, once=False))
    output = capsys.readouterr().out
    assert output.count("Первая строка") == 1
    assert output.count("Вторая строка") == 1
    assert "Статус: completed" in output


def test_none_silent_for_sec_is_not_printed(tmp_path):
    from agent_orchestrator.models import Task
    from agent_orchestrator.progress import ProgressFeed

    feed = ProgressFeed(log_dir=tmp_path, poll_interval_sec=0)
    task = Task(title="t", prompt="p")
    feed.on_wait_progress(task, object(), {"status": "running", "silent_for_sec": None})
    text = (tmp_path / f"{task.id[:8]}.log").read_text(encoding="utf-8")
    assert "статус running" in text
    assert "None" not in text and "тишина" not in text


def _real_step(step_type, state, **extra):
    # Shapes copied from a real Agent-Bridge transcript of an Antigravity run.
    step = {"conversation_id": "c", "step_index": 1, "state": state, "step_type": step_type, **extra}
    return {"type": "tool_call" if step_type == "tool" else "message_chunk", "ts": "t",
            "data": {"payload": {"event": "step_update", "step_update": step}}}


def test_describe_real_transcript_events():
    from agent_orchestrator.progress import describe_transcript_event as describe

    assert describe({"type": "prompt_sent", "data": {"text": "Fix calc", "cmd": ["agy"]}}) == "prompt_sent: Fix calc"
    assert describe({"type": "raw", "data": {"payload": {"event": "init", "init": {"model": "gemini-3.8-flash-high"}}}}) \
        == "старт агента: модель gemini-3.8-flash-high"
    assert describe({"type": "raw", "data": {"payload": {"event": "other"}}}) is None
    view = _real_step("tool", "DONE", tool_name="view_file",
                      tool_info={"name": "view_file", "parameters": {"AbsolutePath": r"C:\s\calc.py"}, "output": "3 lines, 32 bytes"})
    assert describe(view) == r"инструмент view_file C:\s\calc.py [DONE] → 3 lines, 32 bytes"
    run = _real_step("tool", "DONE", tool_name="run_command",
                     tool_info={"parameters": {"CommandLine": "pytest test_calc.py"},
                                "output": "==== test session starts ====\nplatform win32\n\n==== 1 passed in 0.02s ===="})
    assert describe(run) == "инструмент run_command pytest test_calc.py [DONE] → 1 passed in 0.02s"
    assert describe(_real_step("tool", "ACTIVE", tool_name="view_file")) == "инструмент view_file [ACTIVE]"
    assert describe(_real_step("agent_response", "DONE")) == "ответ агента [DONE]"
    assert describe({"type": "turn_end", "data": {"stop_reason": "end_turn"}}) == "конец хода: end_turn"
    assert describe("garbage") is None and describe({"type": "x", "data": "y"}) == "x"


def test_feed_collapses_identical_consecutive_transcript_lines(tmp_path):
    from agent_orchestrator.models import Task
    from agent_orchestrator.progress import ProgressFeed

    events = [_real_step("agent_response", "DONE")] * 5 + [_real_step("tool", "DONE", tool_name="view_file")]

    class Bridge:
        def get_transcript(self, session_id, offset=0, limit=50, kinds=None):
            page = events[offset:offset + limit]
            return {"ok": True, "events": page, "next_offset": offset + len(page)}

    task = Task(title="t", prompt="p", bridge_session_id="sess_1")
    ProgressFeed(log_dir=tmp_path, poll_interval_sec=0).on_wait_progress(task, Bridge(), {"status": "running"})
    lines = (tmp_path / f"{task.id[:8]}.log").read_text(encoding="utf-8").splitlines()
    assert sum("ответ агента [DONE]" in line for line in lines) == 1
    assert any("инструмент view_file [DONE]" in line for line in lines)
