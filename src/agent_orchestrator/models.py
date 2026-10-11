from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @classmethod
    def terminal_statuses(cls) -> set[TaskStatus]:
        return {cls.COMPLETED, cls.FAILED, cls.TIMED_OUT, cls.CANCELLED}

    @property
    def is_terminal(self) -> bool:
        return self in self.terminal_statuses()


class TaskComplexity(str, Enum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    EXPERT = "expert"


class VerificationVerdict(str, Enum):
    PASSED = "passed"
    RETRY_NEEDED = "retry_needed"
    FAILED = "failed"


@dataclass
class ModelSelection:
    model: str
    effort: str
    complexity: TaskComplexity
    reason: str
    is_fallback: bool = False
    is_override: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "complexity": self.complexity.value,
            "reason": self.reason,
            "is_fallback": self.is_fallback,
            "is_override": self.is_override,
        }


@dataclass
class VerificationResult:
    verdict: VerificationVerdict
    feedback: str
    exit_code: int
    raw_output: str
    issues_found: List[str] = field(default_factory=list)
    tests_executed: bool = False
    tests_passed: bool = False
    test_evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "feedback": self.feedback,
            "exit_code": self.exit_code,
            "raw_output": self.raw_output,
            "issues_found": self.issues_found,
            "tests_executed": self.tests_executed,
            "tests_passed": self.tests_passed,
            "test_evidence": self.test_evidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VerificationResult:
        return cls(
            verdict=VerificationVerdict(data.get("verdict", "failed")),
            feedback=data.get("feedback", ""),
            exit_code=data.get("exit_code", 1),
            raw_output=data.get("raw_output", ""),
            issues_found=data.get("issues_found", []),
            tests_executed=data.get("tests_executed", False),
            tests_passed=data.get("tests_passed", False),
            test_evidence=data.get("test_evidence", ""),
        )


@dataclass
class TaskEvent:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    event_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    prompt: str = ""
    acceptance_criteria: str = ""
    cwd: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    complexity: TaskComplexity = TaskComplexity.MODERATE
    model: str = "gemini-3.8-flash-high"
    effort: str = "medium"
    model_reason: str = "default"

    # Agent-Bridge tracking
    bridge_task_id: Optional[str] = None
    bridge_session_id: Optional[str] = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Lifecycle & Retry control
    retry_count: int = 0
    max_retries: int = 3  # Deprecated: retained for SQLite schema compatibility, ignored
    current_attempt: int = 1

    # Stage results
    antigravity_result: Optional[str] = None
    git_diff: Optional[str] = None
    verification_verdict: Optional[VerificationVerdict] = None
    verification_feedback: Optional[str] = None
    error_message: Optional[str] = None

    # Timestamps
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def mark_running(self, bridge_task_id: Optional[str] = None) -> None:
        self.status = TaskStatus.RUNNING
        now = datetime.now(timezone.utc).isoformat()
        if not self.started_at:
            self.started_at = now
        self.updated_at = now
        if bridge_task_id:
            self.bridge_task_id = bridge_task_id

    def mark_completed(self) -> None:
        self.status = TaskStatus.COMPLETED
        now = datetime.now(timezone.utc).isoformat()
        self.finished_at = now
        self.updated_at = now

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error_message = error
        now = datetime.now(timezone.utc).isoformat()
        self.finished_at = now
        self.updated_at = now

    def mark_timed_out(self, error: str = "Task execution timed out") -> None:
        self.status = TaskStatus.TIMED_OUT
        self.error_message = error
        now = datetime.now(timezone.utc).isoformat()
        self.finished_at = now
        self.updated_at = now

    def mark_cancelled(self, reason: str = "Task cancelled by user") -> None:
        self.status = TaskStatus.CANCELLED
        self.error_message = reason
        now = datetime.now(timezone.utc).isoformat()
        self.finished_at = now
        self.updated_at = now

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "acceptance_criteria": self.acceptance_criteria,
            "cwd": self.cwd,
            "status": self.status.value,
            "complexity": self.complexity.value,
            "model": self.model,
            "effort": self.effort,
            "model_reason": self.model_reason,
            "bridge_task_id": self.bridge_task_id,
            "bridge_session_id": self.bridge_session_id,
            "request_id": self.request_id,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "current_attempt": self.current_attempt,
            "antigravity_result": self.antigravity_result,
            "git_diff": self.git_diff,
            "verification_verdict": (
                self.verification_verdict.value if self.verification_verdict else None
            ),
            "verification_feedback": self.verification_feedback,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Task:
        verdict = data.get("verification_verdict")
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", ""),
            prompt=data.get("prompt", ""),
            acceptance_criteria=data.get("acceptance_criteria", ""),
            cwd=data.get("cwd", ""),
            status=TaskStatus(data.get("status", "queued")),
            complexity=TaskComplexity(data.get("complexity", "moderate")),
            model=data.get("model", "gemini-3.8-flash-high"),
            effort=data.get("effort", "medium"),
            model_reason=data.get("model_reason", ""),
            bridge_task_id=data.get("bridge_task_id"),
            bridge_session_id=data.get("bridge_session_id"),
            request_id=data.get("request_id", str(uuid.uuid4())),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            current_attempt=data.get("current_attempt", 1),
            antigravity_result=data.get("antigravity_result"),
            git_diff=data.get("git_diff"),
            verification_verdict=VerificationVerdict(verdict) if verdict else None,
            verification_feedback=data.get("verification_feedback"),
            error_message=data.get("error_message"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )
