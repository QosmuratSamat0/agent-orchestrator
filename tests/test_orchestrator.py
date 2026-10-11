from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_orchestrator.bridge_client import BridgeError
from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import Task, TaskStatus, VerificationVerdict
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.waiter import (
    NonLLMWaiter,
    TaskCancelledError,
    TaskTimeoutError,
)
from tests.conftest import MockBridgeClient, MockCodexVerifier


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


def test_orchestrator_retry_limit_reached(
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




