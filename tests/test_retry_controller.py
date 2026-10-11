from __future__ import annotations

import pytest
from agent_orchestrator.models import (
    Task,
    TaskEvent,
    VerificationResult,
    VerificationVerdict,
)
from agent_orchestrator.retry_controller import RetryController


def test_retry_limits():
    controller = RetryController()
    task = Task(title="Test", prompt="Fix")

    v_retry = VerificationResult(
        verdict=VerificationVerdict.RETRY_NEEDED,
        feedback="Fix test",
        exit_code=0,
        raw_output="",
    )
    v_pass = VerificationResult(
        verdict=VerificationVerdict.PASSED, feedback="", exit_code=0, raw_output=""
    )
    v_fail = VerificationResult(
        verdict=VerificationVerdict.FAILED, feedback="", exit_code=1, raw_output=""
    )

    # Regardless of retry_count, RETRY_NEEDED can always retry
    task.retry_count = 0
    assert controller.can_retry(task, v_retry) is True

    task.retry_count = 1
    assert controller.can_retry(task, v_retry) is True

    task.retry_count = 2
    assert controller.can_retry(task, v_retry) is True

    task.retry_count = 3
    assert controller.can_retry(task, v_retry) is True

    task.retry_count = 100
    assert controller.can_retry(task, v_retry) is True

    # PASSED or FAILED cannot retry
    task.retry_count = 0
    assert controller.can_retry(task, v_pass) is False
    assert controller.can_retry(task, v_fail) is False


def test_build_refinement_prompt():
    controller = RetryController()
    task = Task(title="Calculator", prompt="Implement add and divide")
    verification = VerificationResult(
        verdict=VerificationVerdict.RETRY_NEEDED,
        feedback="ZeroDivisionError not handled",
        exit_code=0,
        raw_output="",
        issues_found=["Handle zero division in div()"],
    )

    prompt = controller.build_refinement_prompt(task, verification)
    assert "Итерация 2" in prompt
    header = prompt.splitlines()[0]
    assert "из" not in header
    assert "ZeroDivisionError not handled" in prompt
    assert "Handle zero division in div()" in prompt
    assert "Implement add and divide" in prompt


def test_final_report_generation():
    controller = RetryController()
    task = Task(
        title="Report Task",
        prompt="Sample task prompt",
        acceptance_criteria="Sample criteria",
        model="gemini-3.8-flash-high",
        retry_count=1,
    )
    task.mark_completed()

    v_res = VerificationResult(
        verdict=VerificationVerdict.PASSED,
        feedback="Everything looks great",
        exit_code=0,
        raw_output="",
        tests_executed=True,
        tests_passed=True,
        test_evidence="python -m pytest (код 0): 5 passed",
    )

    events = [
        TaskEvent(task_id=task.id, event_type="TASK_CREATED", payload={"m": "flash"}),
        TaskEvent(task_id=task.id, event_type="VERIFICATION_COMPLETED", payload={"v": "passed"}),
    ]

    report = controller.generate_final_report(task, last_verification=v_res, events=events)
    assert "ИТОГОВЫЙ ОТЧЁТ ОРКЕСТРАТОРА" in report
    assert "COMPLETED" in report
    assert "gemini-3.8-flash-high" in report
    assert "- **Количество попыток / повторов:** 1" in report
    assert "Everything looks great" in report
    assert "python -m pytest (код 0): 5 passed" in report
    assert "TASK_CREATED" in report
    assert "VERIFICATION_COMPLETED" in report
