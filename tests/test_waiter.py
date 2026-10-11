from __future__ import annotations

import time
from typing import Any, Dict, List
import pytest

from agent_orchestrator.bridge_client import BridgeError
from agent_orchestrator.models import TaskStatus
from agent_orchestrator.waiter import (
    NonLLMWaiter,
    TaskCancelledError,
    TaskTimeoutError,
)
from tests.conftest import MockBridgeClient


def test_waiter_success_immediate(mock_bridge: MockBridgeClient):
    mock_bridge.tasks_status_map["t1"] = {
        "ok": True,
        "task_id": "t1",
        "status": "completed",
        "result_text": "Success",
    }
    waiter = NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=1.0, total_timeout_sec=5.0)

    progress_reports = []
    res = waiter.wait_for_completion("t1", on_progress=lambda p: progress_reports.append(p))
    assert res.get("status") == "completed"
    assert len(progress_reports) == 1


def test_waiter_timeout_raises(mock_bridge: MockBridgeClient):
    # Task stays running forever
    mock_bridge.tasks_status_map["t_slow"] = {
        "ok": True,
        "task_id": "t_slow",
        "status": "running",
    }
    waiter = NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=0.1, total_timeout_sec=0.3)

    with pytest.raises(TaskTimeoutError):
        waiter.wait_for_completion("t_slow")

    # Verify cancel was sent to bridge on timeout
    assert "t_slow" in mock_bridge.cancelled_tasks


def test_waiter_user_cancellation(mock_bridge: MockBridgeClient):
    mock_bridge.tasks_status_map["t_cancel"] = {
        "ok": True,
        "task_id": "t_cancel",
        "status": "running",
    }
    waiter = NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=1.0, total_timeout_sec=10.0)

    cancelled_flag = False

    def check_cancel():
        nonlocal cancelled_flag
        return cancelled_flag

    # Trigger cancel immediately
    cancelled_flag = True

    with pytest.raises(TaskCancelledError):
        waiter.wait_for_completion("t_cancel", should_cancel=check_cancel)

    assert "t_cancel" in mock_bridge.cancelled_tasks


def test_waiter_status_lifecycle_queued_running_completed():
    """Verify waiter distinguishes QUEUED, RUNNING, and COMPLETED properly."""
    class DynamicBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return {"ok": True, "task_id": task_id, "status": "queued"}
            elif self.calls == 2:
                return {"ok": True, "task_id": task_id, "status": "running"}
            else:
                return {"ok": True, "task_id": task_id, "status": "completed", "result_text": "Finished"}

    dyn_bridge = DynamicBridge()
    waiter = NonLLMWaiter(bridge=dyn_bridge, wait_chunk_sec=0.1, total_timeout_sec=5.0)

    statuses_seen = []
    res = waiter.wait_for_completion("t_life", on_progress=lambda p: statuses_seen.append(p.get("status")))

    assert res.get("status") == "completed"
    assert statuses_seen == ["queued", "running", "completed"]
    assert dyn_bridge.calls == 3


def test_waiter_failed_status_returns_result():
    """Verify waiter returns FAILED without treating it as an unhandled exception or success."""
    class FailedBridge(MockBridgeClient):
        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            return {"ok": True, "task_id": task_id, "status": "failed", "error": "Antigravity compilation error"}

    bridge = FailedBridge()
    waiter = NonLLMWaiter(bridge=bridge, wait_chunk_sec=0.5, total_timeout_sec=5.0)

    res = waiter.wait_for_completion("t_fail")
    assert res.get("status") == "failed"
    assert "compilation error" in res.get("error", "")


def test_waiter_cancelled_status_returns_result():
    """Verify waiter returns CANCELLED when bridge reports cancelled."""
    class CancelledBridge(MockBridgeClient):
        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            return {"ok": True, "task_id": task_id, "status": "cancelled", "reason": "aborted by bridge"}

    bridge = CancelledBridge()
    waiter = NonLLMWaiter(bridge=bridge, wait_chunk_sec=0.5, total_timeout_sec=5.0)

    res = waiter.wait_for_completion("t_canc")
    assert res.get("status") == "cancelled"


def test_waiter_transient_bridge_error_with_backoff_recovery():
    """Verify that transient ok=false and exceptions do not cause false completion and recover."""
    class TransientErrorBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.wait_calls = 0

        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            self.wait_calls += 1
            if self.wait_calls == 1:
                # Simulates bridge transient RPC error
                raise BridgeError("Connection reset by peer")
            elif self.wait_calls == 2:
                # Simulates wait_task ok=false
                return {"ok": False, "error": "Bridge worker temporarily busy"}
            else:
                return {"ok": True, "task_id": task_id, "status": "completed", "result_text": "All done"}

        def check_task(self, task_id: str) -> Dict[str, Any]:
            if self.wait_calls == 1:
                # Fallback also fails on first attempt
                return {"ok": False, "error": "Bridge unavailable"}
            return {"ok": True, "task_id": task_id, "status": "running"}

    bridge = TransientErrorBridge()
    # Fast backoff for test speed
    waiter = NonLLMWaiter(
        bridge=bridge,
        wait_chunk_sec=0.1,
        total_timeout_sec=5.0,
        max_consecutive_errors=5,
        base_backoff_sec=0.05,
        max_backoff_sec=0.2,
    )

    res = waiter.wait_for_completion("t_transient")
    assert res.get("status") == "completed"
    assert bridge.wait_calls >= 3


def test_waiter_persistent_bridge_error_raises():
    """Verify that persistent polling failures raise BridgeError once limit is exceeded."""
    class PersistentErrorBridge(MockBridgeClient):
        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            return {"ok": False, "error": "Daemon crashed"}

        def check_task(self, task_id: str) -> Dict[str, Any]:
            return {"ok": False, "error": "Daemon crashed"}

    bridge = PersistentErrorBridge()
    waiter = NonLLMWaiter(
        bridge=bridge,
        wait_chunk_sec=0.1,
        total_timeout_sec=5.0,
        max_consecutive_errors=3,
        base_backoff_sec=0.02,
        max_backoff_sec=0.05,
    )

    with pytest.raises(BridgeError) as exc_info:
        waiter.wait_for_completion("t_dead")

    assert "polling failed permanently" in str(exc_info.value)


def test_waiter_unknown_status_does_not_falsely_complete():
    """Verify that unknown statuses are treated with backoff and do not complete prematurely."""
    class UnknownStatusBridge(MockBridgeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            self.calls += 1
            if self.calls < 3:
                return {"ok": True, "task_id": task_id, "status": "mysterious_state_42"}
            return {"ok": True, "task_id": task_id, "status": "completed"}

    bridge = UnknownStatusBridge()
    waiter = NonLLMWaiter(
        bridge=bridge,
        wait_chunk_sec=0.1,
        total_timeout_sec=5.0,
        max_consecutive_errors=5,
        base_backoff_sec=0.02,
        max_backoff_sec=0.05,
    )

    res = waiter.wait_for_completion("t_unknown")
    assert res.get("status") == "completed"
    assert bridge.calls >= 3


def test_waiter_cancel_during_backoff():
    """Verify that cancellation requested during backoff sleep aborts immediately."""
    class SlowFailBridge(MockBridgeClient):
        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            return {"ok": False, "error": "Transient error"}

        def check_task(self, task_id: str) -> Dict[str, Any]:
            return {"ok": False, "error": "Transient error"}

    bridge = SlowFailBridge()
    # Relatively long backoff
    waiter = NonLLMWaiter(
        bridge=bridge,
        wait_chunk_sec=0.1,
        total_timeout_sec=10.0,
        base_backoff_sec=3.0,
        max_backoff_sec=5.0,
    )

    start = time.time()
    cancelled = False

    def cancel_check():
        # Cancel after 0.3s of sleeping
        return (time.time() - start) > 0.3

    with pytest.raises(TaskCancelledError):
        waiter.wait_for_completion("t_cancel_sleep", should_cancel=cancel_check)

    # Should cancel well before the 3.0s backoff finishes
    elapsed = time.time() - start
    assert elapsed < 1.5
    assert "t_cancel_sleep" in bridge.cancelled_tasks


def test_waiter_total_timeout_during_backoff():
    """Verify that total timeout elapsing during backoff sleep raises TaskTimeoutError promptly."""
    class ContinualErrorBridge(MockBridgeClient):
        def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
            return {"ok": False, "error": "Temporary network glitch"}

        def check_task(self, task_id: str) -> Dict[str, Any]:
            return {"ok": False, "error": "Temporary network glitch"}

    bridge = ContinualErrorBridge()
    waiter = NonLLMWaiter(
        bridge=bridge,
        wait_chunk_sec=0.1,
        total_timeout_sec=0.4,
        base_backoff_sec=2.0,
        max_backoff_sec=5.0,
    )

    start = time.time()
    with pytest.raises(TaskTimeoutError):
        waiter.wait_for_completion("t_timeout_sleep")
    elapsed = time.time() - start
    # Should timeout around 0.4s, well before the 2.0s backoff expires
    assert 0.3 <= elapsed < 1.5
    assert "t_timeout_sleep" in bridge.cancelled_tasks


def test_waiter_default_timeout_is_2700(mock_bridge: MockBridgeClient):
    waiter = NonLLMWaiter(bridge=mock_bridge)
    assert waiter.total_timeout_sec == 2700.0


def test_waiter_total_timeout_sec_override(mock_bridge: MockBridgeClient):
    mock_bridge.tasks_status_map["t_slow_override"] = {
        "ok": True,
        "task_id": "t_slow_override",
        "status": "running",
    }
    # Waiter has large default timeout
    waiter = NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=0.1, total_timeout_sec=300.0)

    start = time.time()
    with pytest.raises(TaskTimeoutError):
        waiter.wait_for_completion("t_slow_override", total_timeout_sec=0.3)
    elapsed = time.time() - start
    assert elapsed < 1.5
    assert "t_slow_override" in mock_bridge.cancelled_tasks

