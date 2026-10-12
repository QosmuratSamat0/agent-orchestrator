from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import pytest

from agent_orchestrator.bridge_client import BridgeError
from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import Task, TaskStatus, VerificationVerdict
from agent_orchestrator.orchestrator import Orchestrator, _attempt_request_id
from agent_orchestrator.repository import TaskRepository
from agent_orchestrator.waiter import (
    NonLLMWaiter,
    TaskCancelledError,
    TaskTimeoutError,
)
from tests.conftest import MockBridgeClient, MockCodexVerifier


@pytest.mark.parametrize("task_request_id", [str(uuid.uuid4()), "legacy-request-id"])
def test_attempt_request_id_is_a_deterministic_uuid(task_request_id):
    request_ids = [_attempt_request_id(task_request_id, attempt) for attempt in range(3)]
    assert request_ids == [_attempt_request_id(task_request_id, attempt) for attempt in range(3)]
    assert len(set(request_ids)) == 3
    assert all(str(uuid.UUID(request_id)) == request_id for request_id in request_ids)
    expected = (
        uuid.uuid5(uuid.UUID(task_request_id), "1")
        if task_request_id != "legacy-request-id"
        else uuid.uuid5(uuid.NAMESPACE_URL, "legacy-request-id:1")
    )
    assert request_ids[1] == str(expected)


def test_mock_bridge_rejects_non_uuid_request_id():
    bridge = MockBridgeClient()
    response = bridge.dispatch_task(agent="antigravity", message="Проверить", cwd=".",
                                    request_id="not-a-uuid")
    assert response == {
        "ok": False,
        "error": "ValueError: request_id must be a UUID",
        "error_type": "ValueError",
    }
    assert bridge.dispatched_tasks == []
    assert bridge.tasks_status_map == {}


def test_dispatch_rejection_records_bridge_error_and_one_report(
    repo, mock_bridge, mock_verifier, tmp_path, monkeypatch
):
    rejection = {
        "ok": False,
        "error": "ValueError: request_id must be a UUID",
        "error_type": "ValueError",
    }
    monkeypatch.setattr(mock_bridge, "dispatch_task", lambda **_: rejection)
    runner = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    task = runner.create_task("Отказ Bridge", "Проверить", cwd=str(tmp_path))

    result = runner.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert rejection["error"] in result.error_message
    assert repo.get_task(task.id).error_message == result.error_message
    assert [(event.event_type, event.payload) for event in repo.get_events(task.id)
            if event.event_type == "DISPATCH_FAILED"] == [
        ("DISPATCH_FAILED", {"error": rejection["error"], "error_type": rejection["error_type"]})
    ]
    reports = list(tmp_path.glob("orchestrator_report_*.md"))
    assert len(reports) == 1
    assert rejection["error"] in reports[0].read_text(encoding="utf-8")
    assert mock_verifier.verification_calls == []


def test_orchestrator_first_attempt_success(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Everything valid")

    task = orchestrator.create_task(
        title="Quick Fix",
        prompt="Add docstring",
        acceptance_criteria="Docstring present",
        cwd=str(tmp_path),
    )
    assert task.status == TaskStatus.QUEUED

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.COMPLETED
    assert finished.retry_count == 0
    assert len(mock_bridge.dispatched_tasks) == 1
    assert len(mock_verifier.verification_calls) == 1


def test_orchestrator_retry_loop_success_on_attempt_2(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    # Attempt 1: RETRY_NEEDED; Attempt 2: PASSED
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Fix typo in line 10")
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Typo fixed")

    task = orchestrator.create_task(
        title="Retry Task",
        prompt="Fix spelling",
        cwd=str(tmp_path),
    )

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.COMPLETED
    assert finished.retry_count == 1
    assert len(mock_bridge.dispatched_tasks) == 2
    assert len(mock_verifier.verification_calls) == 2


def test_orchestrator_unlimited_retries_until_pass(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    # 10 consecutive RETRY_NEEDED verdicts, then 1 PASSED
    for i in range(10):
        mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, f"Issue {i+1}")
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Finally passed")

    task = orchestrator.create_task(
        title="Stubborn Task",
        prompt="Fix tough bug",
        cwd=str(tmp_path),
    )

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.COMPLETED
    assert finished.retry_count == 10
    assert len(mock_bridge.dispatched_tasks) == 11
    assert len(mock_verifier.verification_calls) == 11


def test_orchestrator_failed_verdict_after_retries(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Issue 1")
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Issue 2")
    mock_verifier.set_next_verdict(VerificationVerdict.FAILED, "Fatal architecture error")

    task = orchestrator.create_task(
        title="Failing Task",
        prompt="Fix tough bug",
        cwd=str(tmp_path),
    )

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.FAILED
    assert finished.retry_count == 2
    assert "Fatal architecture error" in finished.error_message
    assert len(mock_bridge.dispatched_tasks) == 3
    assert len(mock_verifier.verification_calls) == 3


def test_orchestrator_idempotency_already_completed(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    task = orchestrator.create_task(
        title="Done Task", prompt="Already done", cwd=str(tmp_path)
    )
    task.mark_completed()
    repo.update_task(task)

    # Re-running task should do nothing and return immediately
    res = orchestrator.run_task(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert len(mock_bridge.dispatched_tasks) == 0
    assert len(mock_verifier.verification_calls) == 0


def test_orchestrator_crash_recovery(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    # Simulate an interrupted task in SQLite
    task = orchestrator.create_task(
        title="Interrupted Task", prompt="Was running", cwd=str(tmp_path)
    )
    task.mark_running(bridge_task_id="mock_interrupted_1")
    repo.update_task(task)

    # Bridge has task as completed
    mock_bridge.tasks_status_map["mock_interrupted_1"] = {
        "ok": True,
        "task_id": "mock_interrupted_1",
        "status": "completed",
        "result_text": "Completed while orchestrator was down",
    }
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Recovered code passed")

    # Run recovery
    recovered_list = orchestrator.recover()
    assert len(recovered_list) == 1
    assert recovered_list[0].id == task.id
    assert recovered_list[0].status == TaskStatus.COMPLETED
    assert len(mock_bridge.dispatched_tasks) == 0


def test_orchestrator_handles_bridge_cancelled(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    task = orchestrator.create_task(
        title="Cancel in Bridge", prompt="Will be cancelled", cwd=str(tmp_path)
    )
    mock_bridge.tasks_status_map["mock_task_1"] = {
        "ok": True,
        "task_id": "mock_task_1",
        "status": "cancelled",
        "reason": "Cancelled by operator",
    }

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.CANCELLED
    assert "Cancelled by operator" in finished.error_message
    # Verifier must NOT be called for cancelled tasks
    assert len(mock_verifier.verification_calls) == 0


def test_orchestrator_handles_bridge_failed(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    task = orchestrator.create_task(
        title="Fail in Bridge", prompt="Will fail in Antigravity", cwd=str(tmp_path)
    )
    mock_bridge.tasks_status_map["mock_task_1"] = {
        "ok": True,
        "task_id": "mock_task_1",
        "status": "failed",
        "error": "Antigravity syntax exception",
    }

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.FAILED
    assert "syntax exception" in finished.error_message
    # Verifier must NOT be called for failed tasks
    assert len(mock_verifier.verification_calls) == 0


def test_orchestrator_waiter_timeout_sqlite_state_and_events(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    class TimeoutWaiter(NonLLMWaiter):
        def wait_for_completion(self, bridge_task_id: str, **kwargs):
            raise TaskTimeoutError("Antigravity timed out after 2700s")

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=TimeoutWaiter(bridge=mock_bridge),
    )
    task = orchestrator.create_task(
        title="Timeout Task", prompt="Will time out", cwd=str(tmp_path)
    )
    finished = orchestrator.run_task(task.id)

    assert finished.status == TaskStatus.TIMED_OUT
    assert "timed out after 2700s" in finished.error_message
    assert len(mock_verifier.verification_calls) == 0

    # Verify SQLite state
    db_task = repo.get_task(task.id)
    assert db_task is not None
    assert db_task.status == TaskStatus.TIMED_OUT

    events = repo.get_events(task.id)
    event_types = [e.event_type for e in events]
    assert "TASK_TIMED_OUT" in event_types


def test_orchestrator_waiter_cancel_sqlite_state_and_events(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    class CancelWaiter(NonLLMWaiter):
        def wait_for_completion(self, bridge_task_id: str, **kwargs):
            raise TaskCancelledError("User aborted")

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=CancelWaiter(bridge=mock_bridge),
    )
    task = orchestrator.create_task(
        title="Cancel Task", prompt="Will be aborted", cwd=str(tmp_path)
    )
    finished = orchestrator.run_task(task.id)

    assert finished.status == TaskStatus.CANCELLED
    assert len(mock_verifier.verification_calls) == 0

    # Verify SQLite state
    db_task = repo.get_task(task.id)
    assert db_task is not None
    assert db_task.status == TaskStatus.CANCELLED

    events = repo.get_events(task.id)
    event_types = [e.event_type for e in events]
    assert "TASK_CANCELLED" in event_types


def test_orchestrator_bridge_error_sqlite_state_and_events(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    class ErrorWaiter(NonLLMWaiter):
        def wait_for_completion(self, bridge_task_id: str, **kwargs):
            raise BridgeError("Daemon process died unexpectedly")

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=ErrorWaiter(bridge=mock_bridge),
    )
    task = orchestrator.create_task(
        title="Error Task", prompt="Bridge will crash", cwd=str(tmp_path)
    )
    finished = orchestrator.run_task(task.id)

    assert finished.status == TaskStatus.FAILED
    assert "Daemon process died unexpectedly" in finished.error_message
    assert len(mock_verifier.verification_calls) == 0

    # Verify SQLite state
    db_task = repo.get_task(task.id)
    assert db_task is not None
    assert db_task.status == TaskStatus.FAILED

    events = repo.get_events(task.id)
    event_types = [e.event_type for e in events]
    assert "TASK_FAILED" in event_types


def test_live_orphan_bridge_error_marks_task_failed(repo, mock_bridge, mock_verifier, tmp_path):
    class OrphanWaiter(NonLLMWaiter):
        def wait_for_completion(self, bridge_task_id, **kwargs):
            raise BridgeError("Задача принадлежит прежнему процессу Agent-Bridge: ожидание и отмена невозможны.")

    orchestrator = Orchestrator(
        repository=repo, bridge=mock_bridge, verifier=mock_verifier,
        waiter=OrphanWaiter(bridge=mock_bridge),
    )
    task = orchestrator.create_task(title="Осиротевшая задача", prompt="Проверка", cwd=str(tmp_path))
    result = orchestrator.run_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert "прежнему процессу Agent-Bridge" in result.error_message
    assert any(e.event_type == "TASK_FAILED" and "прежнему процессу" in e.payload["error"]
               for e in repo.get_events(task.id))
    assert mock_verifier.verification_calls == []


def test_orchestrator_close_is_best_effort(repo, mock_verifier):
    class BrokenCloseBridge(MockBridgeClient):
        def close(self):
            raise RuntimeError("Сбой закрытия")

    orchestrator = Orchestrator(repository=repo, bridge=BrokenCloseBridge(), verifier=mock_verifier)
    orchestrator.close()


def test_cli_handlers_always_close_fake_orchestrator(monkeypatch, tmp_path):
    from agent_orchestrator import cli

    config = OrchestratorConfig(db_path=str(tmp_path / "cli.sqlite"))
    monkeypatch.setattr(cli.OrchestratorConfig, "load", lambda *_: config)
    closed = []
    task = Task(title="Тест", prompt="Проверка", cwd=str(tmp_path))
    task.mark_completed()

    class FakeOrchestrator:
        def __init__(self, config):
            self.progress = None
            self.repo = SimpleNamespace(
                get_task=lambda *_: task, get_active_task=lambda: task,
                list_tasks=lambda **_: [task], get_events=lambda *_: [],
            )
            self.router = SimpleNamespace(available_models=set())
            self.retry_controller = SimpleNamespace(generate_final_report=lambda *_, **__: "Отчёт")

        def create_task(self, **kwargs):
            return task

        def run_task(self, *args, **kwargs):
            return task

        def recover(self):
            return []

        def cancel_task(self, *_, **__):
            return task

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "Orchestrator", FakeOrchestrator)
    args = argparse.Namespace(config=None, title="Тест", prompt="Проверка", criteria="",
                              cwd=str(tmp_path), model=None, effort=None, task_id=task.id, status=None)
    with pytest.raises(SystemExit) as completed_exit:
        cli.cmd_run(args)
    assert completed_exit.value.code == 0
    for handler in (cli.cmd_submit, cli.cmd_status, cli.cmd_list,
                    cli.cmd_recover, cli.cmd_cancel, cli.cmd_models, cli.cmd_report):
        handler(args)
    assert len(closed) == 8

    class FailingOrchestrator(FakeOrchestrator):
        def create_task(self, **kwargs):
            raise RuntimeError("Ошибка создания")

    monkeypatch.setattr(cli, "Orchestrator", FailingOrchestrator)
    with pytest.raises(RuntimeError, match="Ошибка создания"):
        cli.cmd_submit(args)
    assert len(closed) == 9


def test_orchestrator_retry_semantic_counts(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    # 2 retries then passed
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Issue 1")
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Issue 2")
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Fixed")

    task = orchestrator.create_task(
        title="Semantic Task", prompt="Check attempt counts", cwd=str(tmp_path)
    )
    finished = orchestrator.run_task(task.id)

    assert finished.status == TaskStatus.COMPLETED
    assert finished.retry_count == 2
    assert finished.current_attempt == 3
    assert len(mock_bridge.dispatched_tasks) == 3
    request_ids = [entry["request_id"] for entry in mock_bridge.dispatched_tasks]
    assert len(set(request_ids)) == 3
    assert all(str(uuid.UUID(request_id)) == request_id for request_id in request_ids)
    assert request_ids == [_attempt_request_id(task.request_id, attempt) for attempt in range(3)]
    assert len(mock_verifier.verification_calls) == 3


def test_orchestrator_recovery_running_task_resumes_without_redispatch(
    repo, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    class RunningResumeBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.wait_task_calls = []

        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            self.wait_task_calls.append(task_id)
            return {
                "ok": True,
                "task_id": task_id,
                "status": "completed",
                "result_text": "Finished by Antigravity during resume",
            }

    mock_bridge = RunningResumeBridge()
    waiter = NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=0.1, total_timeout_sec=5.0)
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )

    # 1. Create a task that was already running in Bridge before crash
    task = orchestrator.create_task(
        title="Running Task", prompt="Resume work", cwd=str(tmp_path)
    )
    task.mark_running(bridge_task_id="orig_bridge_running_42")
    repo.update_task(task)

    # 2. Bridge check_task reports task is RUNNING in memory
    mock_bridge.tasks_status_map["orig_bridge_running_42"] = {
        "ok": True,
        "task_id": "orig_bridge_running_42",
        "status": "running",
    }
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Recovered run passed")

    # 3. Recover
    recovered = orchestrator.recover()

    # 4. Assert dispatch_task was NOT called again!
    assert len(mock_bridge.dispatched_tasks) == 0

    # 5. Assert wait_for_completion was called with the original bridge_task_id
    assert "orig_bridge_running_42" in mock_bridge.wait_task_calls

    # 6. Assert task is completed with original ID
    assert len(recovered) == 1
    assert recovered[0].id == task.id
    assert recovered[0].bridge_task_id == "orig_bridge_running_42"
    assert recovered[0].status == TaskStatus.COMPLETED

    # 7. Assert SQLite persistence
    db_task = repo.get_task(task.id)
    assert db_task is not None
    assert db_task.status == TaskStatus.COMPLETED
    assert db_task.bridge_task_id == "orig_bridge_running_42"
    assert len(mock_verifier.verification_calls) == 1


def test_orchestrator_recovery_queued_task_resumes_without_redispatch(
    repo, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    class QueuedResumeBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.wait_task_calls = []

        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            self.wait_task_calls.append(task_id)
            return {
                "ok": True,
                "task_id": task_id,
                "status": "completed",
                "result_text": "Finished queued task",
            }

    mock_bridge = QueuedResumeBridge()
    waiter = NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=0.1, total_timeout_sec=5.0)
    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )

    task = orchestrator.create_task(
        title="Queued Task", prompt="Resume queued", cwd=str(tmp_path)
    )
    task.mark_running(bridge_task_id="orig_bridge_queued_77")
    repo.update_task(task)

    mock_bridge.tasks_status_map["orig_bridge_queued_77"] = {
        "ok": True,
        "task_id": "orig_bridge_queued_77",
        "status": "queued",
    }
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED, "Pass")

    recovered = orchestrator.recover()

    assert len(mock_bridge.dispatched_tasks) == 0
    assert "orig_bridge_queued_77" in mock_bridge.wait_task_calls
    assert len(recovered) == 1
    assert recovered[0].status == TaskStatus.COMPLETED


def test_orchestrator_recovery_terminal_states_do_not_restart_task(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    cfg = OrchestratorConfig(max_active_tasks=5)
    orchestrator = Orchestrator(
        config=cfg,
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
    )
    # Task 1: cancelled in bridge
    t_canc = orchestrator.create_task(
        title="Canc Task", prompt="Will be cancelled", cwd=str(tmp_path)
    )
    t_canc.mark_running(bridge_task_id="br_canc_1")
    repo.update_task(t_canc)
    mock_bridge.tasks_status_map["br_canc_1"] = {
        "ok": True,
        "task_id": "br_canc_1",
        "status": "cancelled",
        "reason": "Cancelled in bridge UI",
    }

    # Task 2: failed in bridge
    t_fail = orchestrator.create_task(
        title="Fail Task", prompt="Will be failed", cwd=str(tmp_path)
    )
    t_fail.mark_running(bridge_task_id="br_fail_2")
    repo.update_task(t_fail)
    mock_bridge.tasks_status_map["br_fail_2"] = {
        "ok": True,
        "task_id": "br_fail_2",
        "status": "failed",
        "error": "Antigravity process killed",
    }

    # Task 3: not found in bridge memory (ok=False)
    t_lost = orchestrator.create_task(
        title="Lost Task", prompt="Bridge restarted", cwd=str(tmp_path)
    )
    t_lost.mark_running(bridge_task_id="br_lost_3")
    repo.update_task(t_lost)

    recovered = orchestrator.recover()
    assert len(mock_bridge.dispatched_tasks) == 0
    assert len(mock_verifier.verification_calls) == 0

    assert repo.get_task(t_canc.id).status == TaskStatus.CANCELLED
    assert repo.get_task(t_fail.id).status == TaskStatus.FAILED
    assert repo.get_task(t_lost.id).status == TaskStatus.FAILED


def test_orchestrator_deadline_across_attempts(
    repo, mock_bridge: MockBridgeClient, waiter, tmp_path: Path
):
    current_time = 1000.0

    class AdvancingVerifier(MockCodexVerifier):
        def verify(self, *args, **kwargs):
            nonlocal current_time
            current_time += 1000.0
            return super().verify(*args, **kwargs)

    verifier = AdvancingVerifier(default_verdict=VerificationVerdict.RETRY_NEEDED)

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=verifier,
        waiter=waiter,
        clock=lambda: current_time,
    )

    task = orchestrator.create_task(
        title="Deadline Across Attempts",
        prompt="Will hit deadline during retries",
        cwd=str(tmp_path),
    )

    finished = orchestrator.run_task(task.id)
    assert finished.status == TaskStatus.TIMED_OUT

    db_task = repo.get_task(task.id)
    assert db_task is not None
    assert db_task.status == TaskStatus.TIMED_OUT

    events = repo.get_events(task.id)
    timeout_events = [e for e in events if e.event_type == "TASK_TIMED_OUT"]
    assert len(timeout_events) == 1

    # Deadline was start + 2700 = 3700.
    # Attempt 1: starts at 1000, verifier advances to 2000. Remaining: 1700.
    # Attempt 2: starts at 2000, verifier advances to 3000. Remaining: 700.
    # Attempt 3: starts at 3000, verifier advances to 4000. Remaining: -300 <= 0.
    # After attempt 3 verification, remaining is -300 <= 0, so loop terminates.
    # Total dispatches is 3; no dispatch occurred after the deadline (4000 > 3700).
    assert len(mock_bridge.dispatched_tasks) == 3
    report = (tmp_path / f"orchestrator_report_{task.id[:8]}.md").read_text(encoding="utf-8")
    assert "TIMED_OUT" in report and "Превышен общий лимит" in report


def test_orchestrator_deadline_survives_restart(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    now = 100000.0
    started_epoch = now - 2400.0  # 40 minutes in the past
    started_iso = datetime.fromtimestamp(started_epoch, tz=timezone.utc).isoformat()

    task = Task(
        title="Restart Task",
        prompt="Resume me",
        cwd=str(tmp_path),
        status=TaskStatus.RUNNING,
        started_at=started_iso,
        bridge_task_id="bridge_resume_123",
    )
    repo.create_task(task)

    passed_timeouts = []

    class RecordingWaiter(NonLLMWaiter):
        def wait_for_completion(self, bridge_task_id: str, **kwargs):
            passed_timeouts.append(kwargs.get("total_timeout_sec"))
            return {"ok": True, "status": "completed"}

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=RecordingWaiter(bridge=mock_bridge),
        clock=lambda: now,
    )

    orchestrator.run_task(task.id, resume_running=True)

    assert len(passed_timeouts) == 1
    timeout = passed_timeouts[0]
    assert timeout is not None
    assert 299.0 <= timeout <= 301.0


def test_orchestrator_already_expired_on_resume(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    now = 100000.0
    started_epoch = now - 3000.0  # 50 minutes in the past
    started_iso = datetime.fromtimestamp(started_epoch, tz=timezone.utc).isoformat()

    task = Task(
        title="Expired Task",
        prompt="Already dead",
        cwd=str(tmp_path),
        status=TaskStatus.RUNNING,
        started_at=started_iso,
        bridge_task_id="bridge_expired_99",
    )
    repo.create_task(task)

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=waiter,
        clock=lambda: now,
    )

    finished = orchestrator.run_task(task.id, resume_running=True)
    assert finished.status == TaskStatus.TIMED_OUT
    assert "bridge_expired_99" in mock_bridge.cancelled_tasks
    assert len(mock_bridge.dispatched_tasks) == 0
    assert len(mock_verifier.verification_calls) == 0

    events = repo.get_events(task.id)
    timeout_events = [e for e in events if e.event_type == "TASK_TIMED_OUT"]
    assert len(timeout_events) == 1
    assert timeout_events[0].payload.get("phase") == "before_resume"


def test_orchestrator_verifier_gets_budget(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, tmp_path: Path
):
    start_time = 1000.0
    current_time = start_time

    class AdvancingWaiter(NonLLMWaiter):
        def wait_for_completion(self, bridge_task_id: str, **kwargs):
            nonlocal current_time
            current_time += 500.0
            return {"ok": True, "status": "completed"}

    orchestrator = Orchestrator(
        repository=repo,
        bridge=mock_bridge,
        verifier=mock_verifier,
        waiter=AdvancingWaiter(bridge=mock_bridge),
        clock=lambda: current_time,
    )
    task = orchestrator.create_task(
        title="Budget Task",
        prompt="Check budget",
        cwd=str(tmp_path),
    )

    mock_verifier.set_next_verdict(VerificationVerdict.PASSED)

    orchestrator.run_task(task.id)

    assert len(mock_verifier.verification_calls) == 1
    call = mock_verifier.verification_calls[0]
    timeout_passed = call.get("timeout_sec")
    assert timeout_passed is not None
    assert timeout_passed <= 2200.0


def _stage3_interrupted(orchestrator, tmp_path, bridge_id="old_bridge_id"):
    task = orchestrator.create_task("Восстановление", "Исправить задачу", cwd=str(tmp_path))
    task.mark_running(bridge_task_id=bridge_id)
    orchestrator.repo.update_task(task)
    return task


def test_recover_completed_retries_then_passes(repo, mock_bridge, mock_verifier, tmp_path):
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    mock_bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": "completed"}
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Исправить проверку типов")
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED)

    assert orchestrator.recover()[0].status == TaskStatus.COMPLETED
    assert len(mock_bridge.dispatched_tasks) == 1
    retry = mock_bridge.dispatched_tasks[0]
    assert retry["request_id"] == _attempt_request_id(task.request_id, 1)
    assert "Исправить проверку типов" in retry["message"]
    assert len(mock_verifier.verification_calls) == 2


@pytest.mark.parametrize("verdict,expected", [
    (VerificationVerdict.FAILED, TaskStatus.FAILED),
    (VerificationVerdict.PASSED, TaskStatus.COMPLETED),
])
def test_recover_completed_terminal_verdict(repo, mock_bridge, mock_verifier, tmp_path, verdict, expected):
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    mock_bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": "completed"}
    mock_verifier.set_next_verdict(verdict)
    assert orchestrator.recover()[0].status == expected
    assert mock_bridge.dispatched_tasks == []
    assert len(mock_verifier.verification_calls) == 1


def test_recover_retry_respects_deadline(repo, mock_bridge, tmp_path):
    anchor = time.time()
    ticks = iter(anchor + n * 1000 for n in range(20))
    verifier = MockCodexVerifier(default_verdict=VerificationVerdict.RETRY_NEEDED)
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=verifier, clock=lambda: next(ticks))
    task = _stage3_interrupted(orchestrator, tmp_path)
    mock_bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": "completed"}
    assert orchestrator.recover()[0].status == TaskStatus.TIMED_OUT
    assert repo.get_task(task.id).status == TaskStatus.TIMED_OUT
    assert verifier.verification_calls


def test_recover_running_retries_only_after_wait(repo, tmp_path):
    class RunningBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.wait_calls = 0

        def wait_task(self, task_id, timeout_sec=180.0):
            self.wait_calls += 1
            if self.wait_calls <= 3:
                assert self.dispatched_tasks == []
                return {"ok": True, "status": "running"}
            return {"ok": True, "status": "completed"}

    bridge = RunningBridge()
    verifier = MockCodexVerifier()
    verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Исправить вывод")
    verifier.set_next_verdict(VerificationVerdict.PASSED)
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": "running"}
    assert orchestrator.recover()[0].status == TaskStatus.COMPLETED
    assert bridge.wait_calls >= 4
    assert len(bridge.dispatched_tasks) == 1
    assert len(verifier.verification_calls) == 2


def test_recover_dispatches_pending_retry_without_old_verification(repo, tmp_path):
    verifier = MockCodexVerifier()

    class AssertDispatchFirstBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.check_calls = 0

        def dispatch_task(self, **kwargs):
            assert verifier.verification_calls == []
            return super().dispatch_task(**kwargs)

        def check_task(self, task_id):
            self.check_calls += 1
            return super().check_task(task_id)

    bridge = AssertDispatchFirstBridge()
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    task.retry_count = 1
    task.verification_feedback = "Исправить восстановление"
    repo.update_task(task)
    bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": "completed"}
    repo.record_event(task.id, "TASK_DISPATCHED", {"attempt": 1})
    repo.record_event(task.id, "VERIFICATION_COMPLETED", {"verdict": "retry_needed"})
    repo.record_event(task.id, "RETRY_INITIATED", {"next_attempt": 2})

    assert orchestrator.recover()[0].status == TaskStatus.COMPLETED
    assert bridge.check_calls == 0
    assert len(bridge.dispatched_tasks) == 1
    assert bridge.dispatched_tasks[0]["request_id"] == _attempt_request_id(task.request_id, 1)
    assert "Исправить восстановление" in bridge.dispatched_tasks[0]["message"]
    assert len(verifier.verification_calls) == 1


def test_recover_missing_bridge_records_failure(repo, mock_bridge, mock_verifier, tmp_path):
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    assert orchestrator.recover()[0].status == TaskStatus.FAILED
    assert any(e.event_type == "TASK_FAILED" for e in repo.get_events(task.id))


def test_recover_bridge_exception_records_failure(repo, mock_verifier, tmp_path):
    class BrokenCheckBridge(MockBridgeClient):
        def check_task(self, task_id):
            raise RuntimeError("Bridge недоступен")

    bridge = BrokenCheckBridge()
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=mock_verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    assert orchestrator.recover()[0].status == TaskStatus.FAILED
    assert any(
        e.event_type == "TASK_FAILED" and "Bridge недоступен" in e.payload["error"]
        for e in repo.get_events(task.id)
    )
    assert "Сбой восстановления: Bridge недоступен" in (
        tmp_path / f"orchestrator_report_{task.id[:8]}.md"
    ).read_text(encoding="utf-8")


def test_recover_external_cancel_ignores_bridge_result(temp_db, tmp_path):
    repo = TaskRepository(temp_db)
    other_repo = TaskRepository(temp_db)
    verifier = MockCodexVerifier()

    class CancelOnCheck(MockBridgeClient):
        def check_task(self, task_id):
            other = other_repo.get_task(active.id)
            other.mark_cancelled("Отменено пользователем")
            other_repo.update_task(other)
            return {"ok": True, "status": "completed"}

    bridge = CancelOnCheck()
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=verifier)
    active = _stage3_interrupted(orchestrator, tmp_path)
    assert orchestrator.recover()[0].status == TaskStatus.CANCELLED
    assert repo.get_task(active.id).status == TaskStatus.CANCELLED
    assert verifier.verification_calls == []
    assert "Отменено пользователем" in (
        tmp_path / f"orchestrator_report_{active.id[:8]}.md"
    ).read_text(encoding="utf-8")


def test_completion_write_cannot_overwrite_external_cancel(temp_db, tmp_path):
    other_repo = TaskRepository(temp_db)

    class CancelBeforeCompletion(TaskRepository):
        def update_task(self, task):
            if task.status == TaskStatus.COMPLETED:
                other = other_repo.get_task(task.id)
                other.mark_cancelled("Отменено пользователем")
                other_repo.update_task(other)
            return super().update_task(task)

    repo = CancelBeforeCompletion(temp_db)
    bridge = MockBridgeClient()
    verifier = MockCodexVerifier()
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=verifier)
    task = orchestrator.create_task("Гонка", "Проверить", cwd=str(tmp_path))
    result = orchestrator.run_task(task.id)
    assert result.status == TaskStatus.CANCELLED
    assert repo.get_task(task.id).status == TaskStatus.CANCELLED


@pytest.mark.parametrize("status", ["running", "queued"])
def test_recover_orphaned_task_does_not_contact_owner(repo, tmp_path, status):
    class OrphanedBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.wait_calls = 0

        def wait_task(self, task_id, timeout_sec=180.0):
            self.wait_calls += 1
            raise AssertionError("Ожидание чужой задачи запрещено")

    bridge = OrphanedBridge()
    verifier = MockCodexVerifier()
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    bridge.tasks_status_map[task.bridge_task_id] = {
        "ok": True, "status": status, "from_state": True,
    }

    result = orchestrator.recover()[0]
    assert result.status == repo.get_task(task.id).status == TaskStatus.FAILED
    assert "ожидание и отмена невозможны" in result.error_message
    assert "вручную" in result.error_message
    orphaned = [e for e in repo.get_events(task.id) if e.event_type == "TASK_ORPHANED"]
    assert len(orphaned) == 1
    assert orphaned[0].payload == {"bridge_task_id": task.bridge_task_id, "status": status}
    assert bridge.dispatched_tasks == []
    assert bridge.wait_calls == 0
    assert bridge.cancelled_tasks == []
    assert verifier.verification_calls == []


def test_recover_completed_from_state_retries_then_passes(repo, mock_bridge, mock_verifier, tmp_path):
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    mock_bridge.tasks_status_map[task.bridge_task_id] = {
        "ok": True, "status": "completed", "from_state": True, "result_text": "Готово",
    }
    assert mock_bridge.get_result(task.bridge_task_id)["from_state"] is True
    mock_verifier.set_next_verdict(VerificationVerdict.RETRY_NEEDED, "Исправить тест")
    mock_verifier.set_next_verdict(VerificationVerdict.PASSED)

    assert orchestrator.recover()[0].status == TaskStatus.COMPLETED
    assert len(mock_bridge.dispatched_tasks) == 1
    assert len(mock_verifier.verification_calls) == 2


def test_recover_completed_from_state_without_result_fails(repo, mock_verifier, tmp_path):
    class MissingResultBridge(MockBridgeClient):
        def get_result(self, task_id, max_chars=60000):
            return {"ok": False, "from_state": True, "error": "Результат недоступен"}

    bridge = MissingResultBridge()
    orchestrator = Orchestrator(repository=repo, bridge=bridge, verifier=mock_verifier)
    task = _stage3_interrupted(orchestrator, tmp_path)
    bridge.tasks_status_map[task.bridge_task_id] = {
        "ok": True, "status": "completed", "from_state": True,
    }

    result = orchestrator.recover()[0]
    assert result.status == TaskStatus.FAILED
    assert "Результат недоступен" in result.error_message
    assert any(e.event_type == "TASK_FAILED" for e in repo.get_events(task.id))
    assert mock_verifier.verification_calls == []
    assert bridge.dispatched_tasks == []


@pytest.mark.parametrize(
    "kind, expected_status, reason",
    [
        ("dispatch_exception", TaskStatus.FAILED, "Отправка недоступна"),
        ("dispatch_no_id", TaskStatus.FAILED, "task_id"),
        ("bridge_failed", TaskStatus.FAILED, "Сбой агента"),
        ("bridge_cancelled", TaskStatus.CANCELLED, "Отменено Bridge"),
        ("wait_timeout", TaskStatus.TIMED_OUT, "Время ожидания истекло"),
        ("wait_orphan", TaskStatus.FAILED, "Прежний Bridge"),
        ("result_error", TaskStatus.FAILED, "Результат недоступен"),
        ("verifier_failed", TaskStatus.FAILED, "Проверка отклонена"),
        ("passed", TaskStatus.COMPLETED, "PASSED"),
    ],
)
def test_every_run_exit_writes_one_report(
    repo, tmp_path, kind, expected_status, reason, monkeypatch
):
    bridge = MockBridgeClient()
    verifier = MockCodexVerifier()
    if kind == "dispatch_exception":
        monkeypatch.setattr(bridge, "dispatch_task", lambda **_: (_ for _ in ()).throw(RuntimeError(reason)))
    elif kind == "dispatch_no_id":
        monkeypatch.setattr(bridge, "dispatch_task", lambda **_: {"ok": True})
    elif kind in ("bridge_failed", "bridge_cancelled"):
        original = bridge.dispatch_task
        def dispatch(**kwargs):
            result = original(**kwargs)
            bridge.tasks_status_map[result["task_id"]] = {
                "ok": True,
                "status": "failed" if kind == "bridge_failed" else "cancelled",
                "error": reason,
            }
            return result
        monkeypatch.setattr(bridge, "dispatch_task", dispatch)
    elif kind == "result_error":
        monkeypatch.setattr(bridge, "get_result", lambda *_: {"ok": False, "error": reason})
    elif kind in ("wait_timeout", "wait_orphan"):
        error_type = TaskTimeoutError if kind == "wait_timeout" else BridgeError
        class FailingWaiter:
            def wait_for_completion(self, *_args, **_kwargs):
                raise error_type(reason)
        waiter = FailingWaiter()
    if kind == "verifier_failed":
        verifier.set_next_verdict(VerificationVerdict.FAILED, reason)

    controller = SimpleNamespace(
        generate_final_report=lambda task, verification, events: (
            f"{task.status.value.upper()} {task.error_message or ''} "
            f"{verification.verdict.value.upper() if verification else ''}"
        ),
        build_refinement_prompt=lambda *_: "Повторить",
    )
    report_calls = []
    original_report = controller.generate_final_report
    controller.generate_final_report = lambda *args: (report_calls.append(args), original_report(*args))[1]
    runner = Orchestrator(repository=repo, bridge=bridge, verifier=verifier,
                          waiter=waiter if kind in ("wait_timeout", "wait_orphan") else None,
                          retry_controller=controller)
    task = runner.create_task("Отчёт", "Проверить", cwd=str(tmp_path))
    result = runner.run_task(task.id)
    report = tmp_path / f"orchestrator_report_{task.id[:8]}.md"
    assert result.status == expected_status
    assert len(report_calls) == 1
    assert len(list(tmp_path.glob("orchestrator_report_*.md"))) == 1
    assert result.status.value.upper() in report.read_text(encoding="utf-8")
    assert reason in report.read_text(encoding="utf-8")


@pytest.mark.parametrize("bridge_response, expected_status, reason", [
    ({"ok": False}, TaskStatus.FAILED, "не найдена"),
    ({"ok": True, "status": "failed", "error": "Сбой Bridge"}, TaskStatus.FAILED, "Сбой Bridge"),
    ({"ok": True, "status": "cancelled", "reason": "Отмена Bridge"}, TaskStatus.CANCELLED, "Отмена Bridge"),
    ({"ok": True, "status": "unknown"}, TaskStatus.FAILED, "unknown"),
    ({"ok": True, "status": "running", "from_state": True}, TaskStatus.FAILED, "вручную"),
])
def test_recover_terminal_branch_writes_report(
    repo, mock_bridge, mock_verifier, tmp_path, bridge_response, expected_status, reason
):
    runner = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    task = _stage3_interrupted(runner, tmp_path)
    mock_bridge.tasks_status_map[task.bridge_task_id] = bridge_response
    result = runner.recover()[0]
    report = (tmp_path / f"orchestrator_report_{task.id[:8]}.md").read_text(encoding="utf-8")
    assert result.status == expected_status
    assert result.status.value.upper() in report
    assert reason in report


def test_cancel_without_bridge_writes_report_and_default_notifies(repo, mock_bridge, tmp_path):
    runner = Orchestrator(repository=repo, bridge=mock_bridge)
    first = _stage3_interrupted(runner, tmp_path, "first")
    result = runner.cancel_task(first.id, notify_bridge=False)
    assert result.status == TaskStatus.CANCELLED
    assert mock_bridge.cancelled_tasks == []
    assert "Отменено пользователем." in (
        tmp_path / f"orchestrator_report_{first.id[:8]}.md"
    ).read_text(encoding="utf-8")
    second = runner.create_task("Отмена", "Задача", cwd=str(tmp_path))
    second.mark_running(bridge_task_id="second")
    repo.update_task(second)
    runner.cancel_task(second.id)
    assert mock_bridge.cancelled_tasks == ["second"]


def test_router_is_lazy_for_repository_actions(repo, mock_bridge, tmp_path, monkeypatch):
    from agent_orchestrator import orchestrator as module

    original_router = module.ModelRouter
    calls = []
    def make_router(*args, **kwargs):
        calls.append(1)
        return original_router(*args, **kwargs)

    monkeypatch.setattr(module, "ModelRouter", make_router)
    runner = Orchestrator(repository=repo, bridge=mock_bridge)
    interrupted = repo.create_task(Task(title="Восстановление", prompt="Проверка", cwd=str(tmp_path)))
    interrupted.mark_running(bridge_task_id="previous_bridge")
    repo.update_task(interrupted)
    mock_bridge.tasks_status_map["previous_bridge"] = {
        "ok": True, "status": "running", "from_state": True,
    }
    assert runner.repo.get_task(interrupted.id)
    assert runner.repo.list_tasks()
    assert runner.recover()[0].status == TaskStatus.FAILED
    task = repo.create_task(Task(title="Отмена", prompt="Проверка", cwd=str(tmp_path)))
    assert runner.cancel_task(task.id, notify_bridge=False).status == TaskStatus.CANCELLED
    assert calls == []
    runner.create_task("Новая", "Проверить", cwd=str(tmp_path))
    assert len(calls) == 1


def test_report_write_failure_does_not_change_terminal_status(repo, mock_bridge, mock_verifier, tmp_path, caplog):
    runner = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier)
    missing_dir = tmp_path / "missing"
    task = runner.create_task("Отчёт", "Проверить", cwd=str(missing_dir))
    result = runner.run_task(task.id)
    assert result.status == TaskStatus.COMPLETED
    assert repo.get_task(task.id).status == TaskStatus.COMPLETED
    assert "Не удалось записать итоговый отчёт" in caplog.text






def test_dispatch_never_sends_effort_with_leveled_slug(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier, waiter=waiter)
    task = orchestrator.create_task(title="Leveled", prompt="напиши pytest тесты для эндпоинта", cwd=str(tmp_path))
    # Legacy row: leveled slug with a conflicting stored effort (what broke the second real run).
    task.effort = "medium"
    repo.update_task(task)

    finished = orchestrator.run_task(task.id)

    assert finished.status == TaskStatus.COMPLETED
    assert mock_bridge.dispatched_tasks[0]["model"] == "gemini-3.8-flash-high"
    assert mock_bridge.dispatched_tasks[0]["effort"] is None


def test_dispatch_sends_explicit_effort_for_unleveled_slug(
    repo, mock_bridge: MockBridgeClient, mock_verifier: MockCodexVerifier, waiter, tmp_path: Path
):
    orchestrator = Orchestrator(repository=repo, bridge=mock_bridge, verifier=mock_verifier, waiter=waiter)
    task = orchestrator.create_task(
        title="Claude", prompt="простая задача", model="claude-sonnet-4-6", effort="high", cwd=str(tmp_path)
    )
    orchestrator.run_task(task.id)
    assert mock_bridge.dispatched_tasks[0]["effort"] == "high"


def test_mock_bridge_rejects_leveled_slug_with_effort(mock_bridge: MockBridgeClient):
    import uuid

    resp = mock_bridge.dispatch_task(
        agent="antigravity", message="m", cwd=".", model="gemini-3.8-flash-high",
        effort="medium", request_id=str(uuid.uuid4()),
    )
    assert resp["ok"] is False
    assert "conflicts with --effort" in resp["error"]
    assert mock_bridge.dispatched_tasks == []
