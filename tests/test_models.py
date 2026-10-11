from __future__ import annotations

import pytest
from agent_orchestrator.models import (
    Task,
    TaskComplexity,
    TaskEvent,
    TaskStatus,
    VerificationResult,
    VerificationVerdict,
)


def test_task_status_terminal():
    assert TaskStatus.COMPLETED.is_terminal is True
    assert TaskStatus.FAILED.is_terminal is True
    assert TaskStatus.TIMED_OUT.is_terminal is True
    assert TaskStatus.CANCELLED.is_terminal is True
    assert TaskStatus.QUEUED.is_terminal is False
    assert TaskStatus.RUNNING.is_terminal is False


def test_task_state_transitions():
    task = Task(title="Test", prompt="Fix bug")
    assert task.status == TaskStatus.QUEUED
    assert task.started_at is None
    assert task.finished_at is None

    task.mark_running(bridge_task_id="task_123")
    assert task.status == TaskStatus.RUNNING
    assert task.bridge_task_id == "task_123"
    assert task.started_at is not None

    task.mark_completed()
    assert task.status == TaskStatus.COMPLETED
    assert task.finished_at is not None


def test_task_failed_and_cancelled():
    task1 = Task(title="Fail Test")
    task1.mark_failed("Syntax error")
    assert task1.status == TaskStatus.FAILED
    assert task1.error_message == "Syntax error"
    assert task1.finished_at is not None

    task2 = Task(title="Cancel Test")
    task2.mark_cancelled("Aborted")
    assert task2.status == TaskStatus.CANCELLED
    assert task2.error_message == "Aborted"

    task3 = Task(title="Timeout Test")
    task3.mark_timed_out("Exceeded 2700s")
    assert task3.status == TaskStatus.TIMED_OUT
    assert task3.error_message == "Exceeded 2700s"


def test_task_serialization_roundtrip():
    task = Task(
        title="Sample",
        prompt="Write tests",
        acceptance_criteria="All pass",
        cwd="C:/work",
        complexity=TaskComplexity.MODERATE,
        model="gemini-3.8-flash-high",
        effort="high",
        bridge_task_id="br_99",
        retry_count=2,
        verification_verdict=VerificationVerdict.PASSED,
        verification_feedback="All tests passed.",
    )
    d = task.to_dict()
    restored = Task.from_dict(d)

    assert restored.id == task.id
    assert restored.title == task.title
    assert restored.prompt == task.prompt
    assert restored.acceptance_criteria == task.acceptance_criteria
    assert restored.complexity == TaskComplexity.MODERATE
    assert restored.model == task.model
    assert restored.effort == task.effort
    assert restored.bridge_task_id == "br_99"
    assert restored.retry_count == 2
    assert restored.verification_verdict == VerificationVerdict.PASSED
    assert restored.verification_feedback == "All tests passed."


def test_verification_result_dict():
    res = VerificationResult(
        verdict=VerificationVerdict.RETRY_NEEDED,
        feedback="Missing test for edge case",
        exit_code=1,
        raw_output="Details",
        issues_found=["Edge case missing"],
        tests_executed=True,
        tests_passed=False,
    )
    d = res.to_dict()
    restored = VerificationResult.from_dict(d)
    assert restored.verdict == VerificationVerdict.RETRY_NEEDED
    assert restored.feedback == "Missing test for edge case"
    assert restored.issues_found == ["Edge case missing"]
    assert restored.tests_executed is True
    assert restored.tests_passed is False
