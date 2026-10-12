from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import pytest

from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.mcp_server import MCPServer, OrchestratorTools
from agent_orchestrator.models import Task, TaskStatus, VerificationVerdict
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.repository import TaskRepository
from agent_orchestrator.worker import run_existing
from tests.conftest import MockBridgeClient, MockCodexVerifier


def test_mcp_protocol_and_utf8(tmp_path):
    class FakeTools:
        def get_status(self, task_id):
            return {"id": task_id, "status": "готово"}

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "get_status", "arguments": {"task_id": "задача"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    reader = io.BytesIO(b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8") for r in requests))
    writer = io.BytesIO()
    MCPServer(FakeTools(), reader, writer).serve()
    replies = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4]
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert {tool["name"] for tool in replies[1]["result"]["tools"]} == {
        "start_task", "get_status", "list_tasks", "get_report", "recover_task", "cancel_task"
    }
    assert json.loads(replies[2]["result"]["content"][0]["text"])["status"] == "готово"
    assert replies[3]["result"] == {}


def _tools_with_fake_orchestrator(tmp_path, monkeypatch):
    from agent_orchestrator import mcp_server
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[orchestrator]\ndb_path = "{(tmp_path / "tasks.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    closed = []

    class FakeOrchestrator:
        def __init__(self, config):
            self.repo = TaskRepository(config.db_path)

        def create_task(self, title, prompt, acceptance_criteria, cwd):
            return self.repo.create_task(Task(
                title=title, prompt=prompt, acceptance_criteria=acceptance_criteria, cwd=cwd
            ))

        def cancel_task(self, task_id, *, notify_bridge):
            assert notify_bridge is False
            task = self.repo.get_task(task_id)
            task.mark_cancelled("Отменено пользователем.")
            self.repo.update_task(task)
            return self._finish(task)

        def _finish(self, task):
            (tmp_path / f"orchestrator_report_{task.id[:8]}.md").write_text(
                "Отчёт готов", encoding="utf-8"
            )
            return task

        def close(self):
            closed.append(True)

    monkeypatch.setattr(mcp_server, "Orchestrator", FakeOrchestrator)
    return OrchestratorTools(str(config_file)), closed


def test_mcp_spawns_hidden_worker_and_returns_immediately(tmp_path, monkeypatch):
    from agent_orchestrator import mcp_server

    tools, closed = _tools_with_fake_orchestrator(tmp_path, monkeypatch)
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    created = tools.start_task("Исправить calc.py", str(tmp_path), criteria="Тесты проходят")
    command, kwargs = calls[0]
    assert command[:3] == [sys.executable, "-m", "agent_orchestrator.worker"]
    assert command[3] == created["id"]
    assert command[command.index("--db-path") + 1] == str((tmp_path / "tasks.sqlite").resolve())
    assert command[command.index("--config") + 1] == str((tmp_path / "config.toml").resolve())
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdout"].name.endswith(".worker.log")
    if os.name == "nt":
        assert not kwargs["creationflags"] & subprocess.DETACHED_PROCESS
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kwargs["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    else:
        assert kwargs["start_new_session"] is True
    assert tools.get_status(created["id"])["owner_alive"] is True
    assert tools.list_tasks()["tasks"][0]["id"] == created["id"]
    assert tools.get_report(created["id"])["message"] == "Отчёт ещё не готов."
    cancelled = tools.cancel_task(created["id"])
    assert cancelled["status"] == "cancelled"
    assert tools.get_report(created["id"])["report"] == "Отчёт готов"
    assert len(closed) == 2


def test_mcp_breakaway_fallback_and_spawn_failure(tmp_path, monkeypatch):
    from agent_orchestrator import mcp_server

    tools, _ = _tools_with_fake_orchestrator(tmp_path, monkeypatch)
    flags = []

    def fallback(command, **kwargs):
        flags.append(kwargs.get("creationflags", 0))
        if len(flags) == 1:
            raise OSError("job denied")
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fallback)
    if os.name == "nt":
        task = tools.start_task("Первый", str(tmp_path))
        assert flags[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
        assert not flags[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
        tools.cancel_task(task["id"])

    def fail(*_, **__):
        raise OSError("spawn denied")

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fail)
    server = MCPServer(tools, io.BytesIO(), io.BytesIO())
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "start_task", "arguments": {"prompt": "Второй", "cwd": str(tmp_path)}}})
    assert response["result"]["isError"] is True
    failed = tools.repo.list_tasks(limit=1)[0]
    assert failed.status == TaskStatus.FAILED
    assert "spawn denied" in failed.error_message
    assert (tmp_path / f"orchestrator_report_{failed.id[:8]}.md").is_file()
    assert any(e.event_type == "TASK_FAILED" for e in tools.repo.get_events(failed.id))


@pytest.mark.skipif(os.name != "nt", reason="Проверка консоли доступна только в Windows")
def test_hidden_worker_grandchild_has_no_visible_console(tmp_path):
    from agent_orchestrator.mcp_server import _worker_creation_flags

    result = tmp_path / "window.txt"
    script = tmp_path / "fake_worker.py"
    script.write_text(
        "import subprocess, sys\n"
        "child = '''import ctypes, pathlib, sys\n"
        "kernel = ctypes.windll.kernel32\n"
        "user = ctypes.windll.user32\n"
        "kernel.GetConsoleWindow.restype = ctypes.c_void_p\n"
        "user.IsWindowVisible.argtypes = [ctypes.c_void_p]\n"
        "window = kernel.GetConsoleWindow()\n"
        "pathlib.Path(sys.argv[1]).write_text(str(bool(window and user.IsWindowVisible(window))))\n'''\n"
        "subprocess.run([sys.executable, '-c', child, sys.argv[1]], check=True)\n",
        encoding="utf-8",
    )
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": tmp_path}
    try:
        process = subprocess.Popen([sys.executable, str(script), str(result)],
                                   creationflags=_worker_creation_flags(), **options)
    except OSError:
        process = subprocess.Popen([sys.executable, str(script), str(result)],
                                   creationflags=_worker_creation_flags(breakaway=False), **options)
    timeout = min(15.0, float(os.environ.get("TEST_TIMEOUT_SEC", "180")) / 2)
    try:
        assert process.wait(timeout=timeout) == 0
        assert result.read_text() == "False"
    finally:
        if process.poll() is None:
            process.kill()  # Только собственный тестовый процесс.
            process.wait(timeout=5)


def test_mcp_rejects_invalid_target_and_returns_tool_error(tmp_path):
    tools = OrchestratorTools.__new__(OrchestratorTools)
    tools.config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    tools.repo = TaskRepository(tools.config.db_path)
    server = MCPServer(tools, io.BytesIO(), io.BytesIO())
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "start_task", "arguments": {"prompt": "Сделать", "cwd": "relative/path"}}})
    assert response["result"]["isError"] is True
    assert "абсолютный путь" in response["result"]["content"][0]["text"]
    assert tools.repo.list_tasks() == []


def test_mcp_requires_existing_explicit_config(tmp_path):
    with pytest.raises(FileNotFoundError, match="Файл конфигурации не найден"):
        OrchestratorTools(str(tmp_path / "missing.toml"))


def test_mcp_stdio_process_in_plain_windows_encoding(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        f'[orchestrator]\ndb_path = "{(tmp_path / "tasks.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    env = os.environ.copy()
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run(
        [sys.executable, "-m", "agent_orchestrator.mcp_server", "--config", str(config)],
        input=request.encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    response = json.loads(process.stdout)
    assert response["id"] == 1
    assert response["result"]["tools"][0]["name"] == "start_task"


def test_worker_completes_with_mocks_and_records_owner(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[progress]\nenabled = false\npoll_interval_sec = 0.1\n", encoding="utf-8")
    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    config.progress.enabled = False
    config.progress.poll_interval_sec = 0.1
    bridge = MockBridgeClient()
    repo = TaskRepository(config.db_path)
    task = repo.create_task(Task(title="Проверка", prompt="Исправить", cwd=str(tmp_path)))

    class SlowVerifier(MockCodexVerifier):
        def verify(self, *args, **kwargs):
            before = repo.get_task(task.id).heartbeat_at
            time.sleep(0.35)
            assert repo.get_task(task.id).heartbeat_at != before
            return super().verify(*args, **kwargs)

    verifier = SlowVerifier()

    def factory(config):
        return Orchestrator(config=config, bridge=bridge, verifier=verifier)

    status = run_existing(task.id, str(config_file), config.db_path, orchestrator_factory=factory)
    stored = repo.get_task(task.id)
    assert status == stored.status == TaskStatus.COMPLETED
    assert stored.owner_pid == os.getpid()
    assert stored.heartbeat_at
    assert (tmp_path / f"orchestrator_report_{task.id[:8]}.md").is_file()


def test_run_task_records_owner_and_heartbeats_during_verification(tmp_path):
    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    config.progress.enabled = False
    config.progress.poll_interval_sec = 0.1
    repo = TaskRepository(config.db_path)
    task = repo.create_task(Task(title="CLI задача", prompt="Исправить", cwd=str(tmp_path)))

    class SlowVerifier(MockCodexVerifier):
        def verify(self, *args, **kwargs):
            first = repo.get_task(task.id)
            assert first.owner_pid == os.getpid()
            time.sleep(0.35)
            assert repo.get_task(task.id).heartbeat_at != first.heartbeat_at
            return super().verify(*args, **kwargs)

    orchestrator = Orchestrator(config=config, repository=repo,
                                bridge=MockBridgeClient(), verifier=SlowVerifier())
    try:
        assert orchestrator.run_task(task.id).status == TaskStatus.COMPLETED
    finally:
        orchestrator.close()


def test_worker_failure_marks_task_and_writes_report(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[progress]\nenabled = false\n", encoding="utf-8")
    db_path = str(tmp_path / "tasks.sqlite")
    repo = TaskRepository(db_path)
    task = repo.create_task(Task(title="Сбой", prompt="Проверить", cwd=str(tmp_path)))
    closed = []

    class BrokenOrchestrator:
        def __init__(self, config):
            self.repo = TaskRepository(config.db_path)

        def run_task(self, task_id):
            raise RuntimeError("поддельный сбой")

        def _finish(self, task):
            (tmp_path / f"orchestrator_report_{task.id[:8]}.md").write_text(
                "Отчёт о сбое", encoding="utf-8"
            )

        def close(self):
            closed.append(True)

    status = run_existing(task.id, str(config_file), db_path,
                          orchestrator_factory=BrokenOrchestrator)
    assert status == repo.get_task(task.id).status == TaskStatus.FAILED
    assert "поддельный сбой" in repo.get_task(task.id).error_message
    assert any(e.event_type == "TASK_FAILED" for e in repo.get_events(task.id))
    assert (tmp_path / f"orchestrator_report_{task.id[:8]}.md").is_file()
    assert closed == [True]


def test_dead_owner_status_starts_background_recovery(tmp_path, monkeypatch):
    from agent_orchestrator import mcp_server

    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    config.progress.enabled = False
    repo = TaskRepository(config.db_path)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    task = repo.create_task(Task(
        title="Прервано", prompt="Исправить", cwd=str(tmp_path),
        status=TaskStatus.RUNNING, bridge_task_id="old-bridge-task",
        owner_pid=999999, heartbeat_at=stale,
    ))
    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mcp_server, "Orchestrator", lambda **kwargs:
                        pytest.fail("MCP-запрос не должен запускать синхронное восстановление"))
    tools = OrchestratorTools.__new__(OrchestratorTools)
    tools.config, tools.repo, tools.config_path = config, repo, None
    summary = tools.get_status(task.id)
    assert summary["owner_alive"] is False
    assert "recover_task" in summary["hint"]
    assert tools.list_tasks()["tasks"][0]["owner_alive"] is False
    assert repo.get_task(task.id).status == TaskStatus.RUNNING
    recovered = tools.recover_task(task.id)
    assert recovered["status"] == "running"
    assert "get_status" in recovered["message"]
    assert len(calls) == 1 and "--recover" in calls[0]
    assert repo.get_task(task.id).status == TaskStatus.RUNNING


def test_recover_refuses_live_owner(tmp_path):
    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    repo = TaskRepository(config.db_path)
    task = repo.create_task(Task(
        title="Живая", prompt="Проверить", cwd=str(tmp_path),
        status=TaskStatus.RUNNING, owner_pid=os.getpid(),
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
    ))
    tools = OrchestratorTools.__new__(OrchestratorTools)
    tools.config, tools.repo = config, repo
    assert tools.get_status(task.id)["owner_alive"] is True
    with pytest.raises(ValueError, match="ещё работает"):
        tools.recover_task(task.id)
    assert repo.get_task(task.id).status == TaskStatus.RUNNING


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_task_without_owner_is_unknown_and_cannot_recover(tmp_path, status):
    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    repo = TaskRepository(config.db_path)
    task = repo.create_task(Task(title="Очередь", prompt="Проверить", cwd=str(tmp_path), status=status))
    tools = OrchestratorTools.__new__(OrchestratorTools)
    tools.config, tools.repo = config, repo
    events_before = repo.get_events(task.id)
    assert tools.get_status(task.id)["owner_alive"] is None
    assert tools.list_tasks()["tasks"][0]["owner_alive"] is None
    assert "Владелец неизвестен" in tools.get_status(task.id)["hint"]
    with pytest.raises(ValueError, match="Владелец неизвестен"):
        tools.recover_task(task.id)
    assert repo.get_task(task.id).status == status
    assert repo.get_events(task.id) == events_before


@pytest.mark.parametrize("bridge_status,verdict,expected", [
    ("completed", VerificationVerdict.PASSED, TaskStatus.COMPLETED),
    ("completed", VerificationVerdict.RETRY_NEEDED, TaskStatus.FAILED),
    ("cancelled", None, TaskStatus.CANCELLED),
    ("failed", None, TaskStatus.FAILED),
])
def test_recovery_worker_never_dispatches(tmp_path, bridge_status, verdict, expected):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[progress]\nenabled = false\n", encoding="utf-8")
    config = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    config.progress.enabled = False
    repo = TaskRepository(config.db_path)
    task = repo.create_task(Task(
        title="Нужны доработки", prompt="Исправить", cwd=str(tmp_path),
        status=TaskStatus.RUNNING, bridge_task_id="completed-bridge-task",
    ))
    bridge = MockBridgeClient()
    bridge.tasks_status_map["completed-bridge-task"] = {
        "ok": True, "status": bridge_status, "from_state": True,
        "result_text": "Результат поддельного агента",
    }
    if bridge_status == "failed":
        bridge.tasks_status_map["completed-bridge-task"]["error"] = "bridge_restarted"
    verifier = MockCodexVerifier()
    if verdict:
        verifier.set_next_verdict(verdict, "Исправить ещё раз")

    def factory(config):
        return Orchestrator(config=config, bridge=bridge, verifier=verifier)

    status = run_existing(task.id, str(config_file), config.db_path,
                          recover=True, orchestrator_factory=factory)
    assert status == repo.get_task(task.id).status == expected
    assert bridge.dispatched_tasks == []
    assert len(verifier.verification_calls) == (1 if verdict else 0)


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "unknown"])
def test_protocol_version_selection(version):
    server = MCPServer(None, io.BytesIO(), io.BytesIO())
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": version}})
    assert response["result"]["protocolVersion"] == (
        version if version != "unknown" else "2025-06-18"
    )


def test_protocol_skips_blank_lines_and_responses_and_rejects_array():
    reader = io.BytesIO(b'\n  \n[1,2]\n{"jsonrpc":"2.0","id":2,"result":{}}\n')
    writer = io.BytesIO()
    MCPServer(None, reader, writer).serve()
    responses = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == -32600


def test_old_database_migrates_owner_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    TaskRepository(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN owner_pid")
        conn.execute("ALTER TABLE tasks DROP COLUMN heartbeat_at")
    repo = TaskRepository(db)
    task = repo.create_task(Task(title="Старая база", prompt="Проверить", cwd=str(tmp_path)))
    repo.touch_owner(task.id, os.getpid())
    assert repo.get_task(task.id).owner_pid == os.getpid()


def test_killed_fake_worker_is_detected_and_recovered(tmp_path, monkeypatch):
    from agent_orchestrator import mcp_server

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[orchestrator]\ndb_path = "{(tmp_path / "tasks.sqlite").as_posix()}"\n'
        '[progress]\nenabled = false\npoll_interval_sec = 0.1\n', encoding="utf-8",
    )
    config = OrchestratorConfig.load(config_file)
    repo = TaskRepository(config.db_path)
    task = repo.create_task(Task(title="Тестовый worker", prompt="Проверить", cwd=str(tmp_path)))
    script = """
import sys, time
from agent_orchestrator.worker import run_existing
from agent_orchestrator.repository import TaskRepository
class FakeOrchestrator:
    def __init__(self, config):
        self.repo = TaskRepository(config.db_path)
    def run_task(self, task_id):
        task = self.repo.get_task(task_id)
        task.mark_running('fake-bridge-task')
        self.repo.update_task(task)
        self.repo.touch_owner(task_id, __import__('os').getpid())
        time.sleep(60)
        return task
    def close(self):
        pass
run_existing(sys.argv[1], sys.argv[2], sys.argv[3], orchestrator_factory=FakeOrchestrator)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen(
        [sys.executable, "-c", script, task.id, str(config_file), config.db_path],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    timeout = min(10.0, float(os.environ.get("TEST_TIMEOUT_SEC", "180")) / 2)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = repo.get_task(task.id)
            if current.status == TaskStatus.RUNNING and current.owner_pid == proc.pid:
                break
            time.sleep(0.05)
        else:
            pytest.fail("Поддельный worker не записал PID вовремя")

        if os.name == "nt":
            killed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, capture_output=True,
            )
            if killed.returncode != 0:
                proc.terminate()  # Only the process started above, for test cleanup.
                proc.wait(timeout=5)
                pytest.skip("Среда запрещает taskkill для дерева собственного тестового worker")
        else:
            proc.terminate()
        proc.wait(timeout=5)

        bridge = MockBridgeClient()
        bridge.tasks_status_map["fake-bridge-task"] = {
            "ok": True, "status": "cancelled", "reason": "Поддельный worker остановлен"
        }
        def factory(config):
            return Orchestrator(config=config, bridge=bridge, verifier=MockCodexVerifier())
        calls = []
        def fake_popen(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(pid=999998)
        monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
        tools = OrchestratorTools(str(config_file))
        assert tools.get_status(task.id)["owner_alive"] is False
        assert tools.recover_task(task.id)["status"] == "running"
        assert len(calls) == 1 and "--recover" in calls[0]
        assert run_existing(task.id, str(config_file), config.db_path,
                            recover=True, orchestrator_factory=factory) == TaskStatus.CANCELLED
        assert bridge.dispatched_tasks == []
    finally:
        if proc.poll() is None:
            proc.terminate()  # Only the process started above.
            proc.wait(timeout=5)
