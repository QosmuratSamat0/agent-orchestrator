from __future__ import annotations

import tempfile
import faulthandler
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from agent_orchestrator.bridge_client import BridgeClient
from agent_orchestrator.codex_verifier import CodexVerifier
from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import (
    Task,
    TaskComplexity,
    TaskStatus,
    VerificationResult,
    VerificationVerdict,
)
from agent_orchestrator.model_router import ModelRouter
from agent_orchestrator.repository import TaskRepository
from agent_orchestrator.retry_controller import RetryController
from agent_orchestrator.waiter import NonLLMWaiter


@pytest.fixture(autouse=True)
def test_safety_guard(monkeypatch):
    # Не обращаться к реальному agy при создании ModelRouter в тестах.
    monkeypatch.setattr(ModelRouter, "_load_available_models_from_cli", lambda self: None)
    timeout = float(os.environ.get("TEST_TIMEOUT_SEC", "180"))
    faulthandler.dump_traceback_later(timeout, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


class MockBridgeClient(BridgeClient):
    """Mock Agent-Bridge client for unit testing without spawning background servers."""

    def __init__(self):
        super().__init__(command="mock-bridge")
        self.dispatched_tasks: List[Dict[str, Any]] = []
        self.tasks_status_map: Dict[str, Dict[str, Any]] = {}
        self.cancelled_tasks: List[str] = []
        self._counter = 0

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def dispatch_task(
        self,
        agent: str,
        message: str,
        cwd: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        title: Optional[str] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._counter += 1
        task_id = f"mock_task_{self._counter}"
        sess_id = session_id or f"mock_sess_{self._counter}"
        entry = {
            "task_id": task_id,
            "session_id": sess_id,
            "agent": agent,
            "message": message,
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "title": title,
            "request_id": request_id,
        }
        self.dispatched_tasks.append(entry)
        # Default mock status is completed if not preset
        if task_id not in self.tasks_status_map:
            self.tasks_status_map[task_id] = {
                "ok": True,
                "task_id": task_id,
                "status": "completed",
                "result_text": f"Mock result for {task_id}",
                "files_changed": ["test.py"],
            }
        return {"ok": True, "task_id": task_id, "session_id": sess_id}

    def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
        return self.tasks_status_map.get(
            task_id, {"ok": True, "task_id": task_id, "status": "completed"}
        )

    def check_task(self, task_id: str) -> Dict[str, Any]:
        if task_id in self.tasks_status_map:
            return self.tasks_status_map[task_id]
        return {"ok": False, "error": f"unknown task {task_id}"}

    def get_result(self, task_id: str, max_chars: int = 60000) -> Dict[str, Any]:
        res = self.tasks_status_map.get(task_id, {})
        result = {
            "ok": True,
            "task_id": task_id,
            "result_text": res.get("result_text", "Result"),
            "files_changed": res.get("files_changed", []),
        }
        if "from_state" in res:
            result["from_state"] = res["from_state"]
        return result

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        self.cancelled_tasks.append(task_id)
        if task_id in self.tasks_status_map:
            self.tasks_status_map[task_id]["status"] = "cancelled"
        return {"ok": True, "cancelled": True}


class MockCodexVerifier(CodexVerifier):
    """Mock Codex Verifier returning controllable verdicts."""

    def __init__(self, default_verdict: VerificationVerdict = VerificationVerdict.PASSED):
        super().__init__()
        self.default_verdict = default_verdict
        self.verification_calls: List[Dict[str, Any]] = []
        self.verdicts_queue: List[VerificationResult] = []

    def set_next_verdict(self, verdict: VerificationVerdict, feedback: str = "") -> None:
        self.verdicts_queue.append(
            VerificationResult(
                verdict=verdict,
                feedback=feedback or f"Mock feedback for {verdict.value}",
                exit_code=0 if verdict == VerificationVerdict.PASSED else 1,
                raw_output=f"VERDICT: {verdict.value.upper()}\nFEEDBACK: {feedback}",
                issues_found=[feedback] if feedback else [],
            )
        )

    def capture_git_diff(self, cwd: str | Path) -> str:
        return "diff --git a/test.py b/test.py\n+ # Added feature"

    def verify(
        self,
        original_prompt: str,
        acceptance_criteria: str,
        antigravity_result: str,
        cwd: str,
        attempt: int = 1,
        timeout_sec: Optional[float] = None,
    ) -> VerificationResult:
        call = {
            "original_prompt": original_prompt,
            "acceptance_criteria": acceptance_criteria,
            "antigravity_result": antigravity_result,
            "cwd": cwd,
            "attempt": attempt,
            "timeout_sec": timeout_sec,
        }
        self.verification_calls.append(call)

        if self.verdicts_queue:
            return self.verdicts_queue.pop(0)

        return VerificationResult(
            verdict=self.default_verdict,
            feedback=f"Verdict: {self.default_verdict.value}",
            exit_code=0 if self.default_verdict == VerificationVerdict.PASSED else 1,
            raw_output=f"VERDICT: {self.default_verdict.value.upper()}\nFEEDBACK: Looks good",
            tests_executed=True,
            tests_passed=(self.default_verdict == VerificationVerdict.PASSED),
        )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_orchestrator.sqlite"


@pytest.fixture
def repo(temp_db: Path) -> TaskRepository:
    return TaskRepository(temp_db)


@pytest.fixture
def mock_bridge() -> MockBridgeClient:
    return MockBridgeClient()


@pytest.fixture
def mock_verifier() -> MockCodexVerifier:
    return MockCodexVerifier()


@pytest.fixture
def model_router() -> ModelRouter:
    return ModelRouter()


@pytest.fixture
def retry_controller() -> RetryController:
    return RetryController()


@pytest.fixture
def waiter(mock_bridge: MockBridgeClient) -> NonLLMWaiter:
    return NonLLMWaiter(bridge=mock_bridge, wait_chunk_sec=1.0, total_timeout_sec=5.0)
