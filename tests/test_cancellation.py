from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from agent_orchestrator.models import ModelSelection, TaskComplexity, TaskStatus, VerificationVerdict
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.repository import TaskRepository
from agent_orchestrator.waiter import NonLLMWaiter, TaskTimeoutError
from tests.conftest import MockBridgeClient, MockCodexVerifier


def make_orchestrator(tmp_path, bridge=None, verifier=None, waiter=None):
    bridge = bridge or MockBridgeClient()
    verifier = verifier or MockCodexVerifier()
    db_path = tmp_path / "tasks.sqlite"
    repo = TaskRepository(db_path)
    router = Mock()
    router.select_model.return_value = ModelSelection(
        model="mock-model", effort="medium", complexity=TaskComplexity.SIMPLE, reason="test"
    )
    runner = Orchestrator(repository=repo, bridge=bridge, verifier=verifier, router=router, waiter=waiter)
    canceller = Orchestrator(repository=TaskRepository(db_path), bridge=bridge, verifier=verifier, router=router)
    return runner, canceller, bridge, verifier


def interrupted_task(runner, tmp_path):
    task = runner.create_task("Возобновление", "Проверить", cwd=str(tmp_path))
    task.mark_running(bridge_task_id="original_bridge_id")
    runner.repo.update_task(task)
    return task


@pytest.mark.parametrize("status", ["in_progress", "executing", "working", "processing", "pending", "scheduled", "waiting"])
def test_recovery_resumes_original_bridge_task(tmp_path, status):
    class WaitingBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.wait_ids = []

        def wait_task(self, task_id, timeout_sec=180.0):
            self.wait_ids.append(task_id)
            state = status if len(self.wait_ids) <= 2 else "completed"
            return {"ok": True, "task_id": task_id, "status": state}

    bridge = WaitingBridge()
    runner, _, _, verifier = make_orchestrator(tmp_path, bridge=bridge)
    task = interrupted_task(runner, tmp_path)
    bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": status}
    recovered = runner.recover()
    assert recovered[0].status == TaskStatus.COMPLETED
    assert bridge.wait_ids == [task.bridge_task_id] * 3
    assert bridge.dispatched_tasks == []
    assert len(verifier.verification_calls) == 1


@pytest.mark.parametrize("status", ["succeeded", "success", "finished", "done"])
def test_recovery_verifies_completed_alias(tmp_path, status):
    runner, _, bridge, verifier = make_orchestrator(tmp_path)
    task = interrupted_task(runner, tmp_path)
    bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": status}
    assert runner.recover()[0].status == TaskStatus.COMPLETED
    assert bridge.dispatched_tasks == []
    assert len(verifier.verification_calls) == 1


@pytest.mark.parametrize("status,expected", [("canceled", TaskStatus.CANCELLED), ("killed", TaskStatus.CANCELLED), ("aborted", TaskStatus.CANCELLED), ("error", TaskStatus.FAILED), ("failed", TaskStatus.FAILED), ("weird_state", TaskStatus.FAILED)])
def test_recovery_terminal_and_unknown_aliases(tmp_path, status, expected):
    runner, _, bridge, _ = make_orchestrator(tmp_path)
    task = interrupted_task(runner, tmp_path)
    bridge.tasks_status_map[task.bridge_task_id] = {"ok": True, "status": status}
    assert runner.recover()[0].status == expected
    assert bridge.dispatched_tasks == []
    if status == "weird_state":
        events = runner.repo.get_events(task.id)
        assert any(e.event_type == "TASK_FAILED" and e.payload["status"] == status for e in events)
        assert status in runner.repo.get_task(task.id).error_message


def test_external_cancel_during_wait(tmp_path):
    class CancelOnWait(MockBridgeClient):
        def wait_task(self, task_id, timeout_sec=180.0):
            if not hasattr(self, "cancelled_once"):
                self.cancelled_once = True
                canceller.cancel_task(active_task.id)
            return {"ok": True, "task_id": task_id, "status": "completed"}

    bridge = CancelOnWait()
    runner, canceller, _, verifier = make_orchestrator(tmp_path, bridge=bridge)
    active_task = runner.create_task("Отмена", "Ждать", cwd=str(tmp_path))
    finished = runner.run_task(active_task.id)
    assert finished.status == runner.repo.get_task(active_task.id).status == TaskStatus.CANCELLED
    assert verifier.verification_calls == []
    assert len(bridge.dispatched_tasks) == 1
    assert bridge.dispatched_tasks[0]["task_id"] in bridge.cancelled_tasks
    assert sum(e.event_type in ("USER_CANCELLED", "TASK_CANCELLED") for e in runner.repo.get_events(active_task.id)) == 1


@pytest.mark.parametrize("verdict", [VerificationVerdict.RETRY_NEEDED, VerificationVerdict.PASSED])
def test_external_cancel_during_verification(tmp_path, verdict):
    class CancelOnVerify(MockCodexVerifier):
        def verify(self, *args, **kwargs):
            canceller.cancel_task(active_task.id)
            return super().verify(*args, **kwargs)

    verifier = CancelOnVerify()
    verifier.set_next_verdict(verdict)
    runner, canceller, bridge, _ = make_orchestrator(tmp_path, verifier=verifier)
    active_task = runner.create_task("Отмена", "Проверить", cwd=str(tmp_path))
    assert runner.run_task(active_task.id).status == TaskStatus.CANCELLED
    assert runner.repo.get_task(active_task.id).status == TaskStatus.CANCELLED
    assert len(bridge.dispatched_tasks) == 1
    assert not any(e.event_type == "RETRY_INITIATED" for e in runner.repo.get_events(active_task.id))


def test_should_cancel_reaches_real_waiter(tmp_path):
    class RunningBridge(MockBridgeClient):
        def wait_task(self, task_id, timeout_sec=180.0):
            canceller.cancel_task(active_task.id)
            return {"ok": True, "task_id": task_id, "status": "running"}

    bridge = RunningBridge()
    waiter = NonLLMWaiter(bridge=bridge, wait_chunk_sec=0.1)
    runner, canceller, _, _ = make_orchestrator(tmp_path, bridge=bridge, waiter=waiter)
    active_task = runner.create_task("Отмена", "Ждать", cwd=str(tmp_path))
    start = time.monotonic()
    assert runner.run_task(active_task.id).status == TaskStatus.CANCELLED
    assert time.monotonic() - start < 2
    assert bridge.dispatched_tasks[0]["task_id"] in bridge.cancelled_tasks


def test_cancel_completed_is_noop(tmp_path):
    runner, _, bridge, _ = make_orchestrator(tmp_path)
    task = runner.create_task("Готово", "Готово", cwd=str(tmp_path))
    task.mark_completed()
    runner.repo.update_task(task)
    before = runner.repo.get_task(task.id)
    events_before = runner.repo.get_events(task.id)
    after = runner.cancel_task(task.id)
    assert after == before
    assert runner.repo.get_events(task.id) == events_before
    assert bridge.cancelled_tasks == []


def test_backoff_uses_override_budget(tmp_path):
    class BrokenBridge(MockBridgeClient):
        def wait_task(self, task_id, timeout_sec=180.0):
            raise RuntimeError("broken")

        def check_task(self, task_id):
            raise RuntimeError("broken")

    bridge = BrokenBridge()
    waiter = NonLLMWaiter(bridge, total_timeout_sec=2700, base_backoff_sec=16)
    start = time.monotonic()
    with pytest.raises(TaskTimeoutError):
        waiter.wait_for_completion("id", total_timeout_sec=1.0)
    assert time.monotonic() - start < 5
