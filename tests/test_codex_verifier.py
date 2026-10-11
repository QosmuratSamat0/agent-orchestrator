from __future__ import annotations

import subprocess
from pathlib import Path
import pytest
from agent_orchestrator.codex_verifier import CodexVerifier
from agent_orchestrator.models import VerificationVerdict


def test_build_verification_prompt():
    verifier = CodexVerifier()
    prompt = verifier.build_verification_prompt(
        original_prompt="Create user service",
        acceptance_criteria="Pass unit tests",
        antigravity_result="Files created",
        git_diff="+ def create_user(): pass",
        attempt=1,
    )

    assert "Create user service" in prompt
    assert "Pass unit tests" in prompt
    assert "Files created" in prompt
    assert "+ def create_user(): pass" in prompt
    assert "VERDICT:" in prompt
    assert "TESTS_STATUS:" in prompt
    assert "RETRY_NEEDED" in prompt


def test_parse_verdict_passed_clean():
    verifier = CodexVerifier()
    raw = """
    Я проверил изменения в кодовой базе. Все тесты выполнены успешно:
    ============================= 5 passed in 0.20s =============================
    
    TESTS_STATUS: PASSED
    VERDICT: PASSED
    FEEDBACK:
    - Замечаний нет, функционал реализован корректно.
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    assert res.verdict == VerificationVerdict.PASSED
    assert res.exit_code == 0
    assert res.tests_executed is True
    assert res.tests_passed is True
    assert "Замечаний нет" in res.feedback


def test_nonzero_exit_code_overrides_passed_to_failed():
    """Requirement: Не допускать PASSED, если Codex завершился с ненулевым exit code."""
    verifier = CodexVerifier()
    raw = """
    Всё отлично, код правильный.
    ============================= 5 passed in 0.20s =============================
    TESTS_STATUS: PASSED
    VERDICT: PASSED
    FEEDBACK:
    - Всё хорошо.
    """
    # Exit code is non-zero (process crashed or error returned)
    res = verifier.parse_verification_output(raw, exit_code=1)
    assert res.verdict == VerificationVerdict.FAILED
    assert any("кодом ошибки 1" in issue or "PASSED аннулирован" in issue for issue in res.issues_found)


def test_missing_verdict_must_not_be_passed():
    """Requirement: Не считать проверку успешной, если в ответе нет явного валидного вердикта."""
    verifier = CodexVerifier()
    raw = """
    Код выглядит отлично. Все файлы на месте, логика чистая.
    Ошибок не обнаружено.
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    # Must NOT be PASSED
    assert res.verdict != VerificationVerdict.PASSED
    assert res.verdict == VerificationVerdict.FAILED
    assert any("Отсутствует явный валидный вердикт" in issue for issue in res.issues_found)


def test_invalid_verdict_tag_treated_as_failed():
    """Requirement: Некорректный вердикт должен приводить к ошибке."""
    verifier = CodexVerifier()
    raw = """
    VERDICT: ALMOST_GOOD
    FEEDBACK:
    - Надо подумать.
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    assert res.verdict == VerificationVerdict.FAILED
    assert any("Отсутствует явный валидный вердикт" in issue for issue in res.issues_found)


def test_failed_tests_override_passed_to_retry_needed():
    """Requirement: Проваленные тесты не должны позволять выставить PASSED."""
    verifier = CodexVerifier()
    raw = """
    Код написан, но unit-тесты упали с ошибкой:
    === 1 failed, 3 passed in 0.12s ===
    
    TESTS_STATUS: FAILED
    VERDICT: PASSED
    FEEDBACK:
    - Тесты упали.
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    assert res.tests_executed is True
    assert res.tests_passed is False
    # Even though text said VERDICT: PASSED, failed tests must override to RETRY_NEEDED
    assert res.verdict == VerificationVerdict.RETRY_NEEDED
    assert any("Зафиксированы проваленные тесты" in issue for issue in res.issues_found)


def test_incidental_words_do_not_falsely_mark_tests_executed_or_passed():
    """Requirement: Не определять успешность тестов по простому наличию слов test или pytest."""
    verifier = CodexVerifier()
    raw = """
    This task was a great test of skills, and we discussed pytest best practices in docstrings.
    
    VERDICT: RETRY_NEEDED
    FEEDBACK:
    - Please implement edge case handling in parse().
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    # The words 'test' and 'pytest' appeared casually, but no tests were executed
    assert res.tests_executed is False
    assert res.tests_passed is False
    assert res.verdict == VerificationVerdict.RETRY_NEEDED


def test_tests_status_passed_without_runner_evidence_is_not_trusted():
    """Requirement: Codex возвращает TESTS_STATUS: PASSED, но нет подтверждений запуска тестового раннера."""
    verifier = CodexVerifier()
    raw = """
    Я посмотрел на код, он кажется рабочим.
    
    TESTS_STATUS: PASSED
    VERDICT: PASSED
    FEEDBACK:
    - Всё в порядке.
    """
    # Case A: Tests were required by prompt/criteria
    res_req = verifier.parse_verification_output(
        raw, exit_code=0, criteria="Unit-тесты должны успешно проходить", prompt="Напиши модуль"
    )
    # We do NOT trust the model's claim without evidence:
    assert res_req.tests_executed is False
    assert res_req.tests_passed is False
    # And since tests were required by criteria, VERDICT cannot be PASSED!
    assert res_req.verdict == VerificationVerdict.RETRY_NEEDED
    assert any("отсутствуют подтверждения реального запуска" in issue for issue in res_req.issues_found)

    # Case B: Tests were NOT required by prompt/criteria
    res_noreq = verifier.parse_verification_output(
        raw, exit_code=0, criteria="Корректный README", prompt="Исправь опечатку в заголовке"
    )
    # Still not marked as tests_passed because no runner was run:
    assert res_noreq.tests_executed is False
    assert res_noreq.tests_passed is False
    # Allowed PASSED for non-test task, but tests were not checked
    assert res_noreq.verdict == VerificationVerdict.PASSED


def test_tests_status_not_run_when_tests_required_overrides_to_retry_needed():
    """Requirement: TESTS_STATUS: NOT_RUN при задании с тестами запрещает PASSED."""
    verifier = CodexVerifier()
    raw = """
    Изменения сделаны, но тесты не запускались.
    
    TESTS_STATUS: NOT_RUN
    VERDICT: PASSED
    FEEDBACK:
    - Код написан.
    """
    res = verifier.parse_verification_output(
        raw, exit_code=0, criteria="Обязательно запустить pytest и проверить тесты"
    )
    assert res.tests_executed is False
    assert res.tests_passed is False
    assert res.verdict == VerificationVerdict.RETRY_NEEDED
    assert any("Критерии задачи требуют успешных тестов" in issue for issue in res.issues_found)


def test_tests_status_not_run_when_tests_not_required_allows_passed():
    """Requirement: TESTS_STATUS: NOT_RUN при задании без тестов разрешает PASSED, не заявляя проверку тестов."""
    verifier = CodexVerifier()
    raw = """
    Изменения сделаны в текстовом файле.
    
    TESTS_STATUS: NOT_RUN
    VERDICT: PASSED
    FEEDBACK:
    - Замечаний нет.
    """
    res = verifier.parse_verification_output(
        raw, exit_code=0, criteria="Форматирование файла документации", prompt="Исправь опечатку"
    )
    assert res.tests_executed is False
    assert res.tests_passed is False
    assert res.verdict == VerificationVerdict.PASSED


def test_real_pytest_pass_output_detected():
    verifier = CodexVerifier()
    raw = """
    Запуск pytest в директории:
    tests/test_app.py ....
    ============================= 4 passed in 0.45s =============================
    
    TESTS_STATUS: PASSED
    VERDICT: PASSED
    FEEDBACK:
    - Все 4 теста пройдены.
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    assert res.tests_executed is True
    assert res.tests_passed is True
    assert res.verdict == VerificationVerdict.PASSED


def test_real_pytest_fail_output_detected():
    verifier = CodexVerifier()
    raw = """
    tests/test_app.py ..F
    ============================= 1 failed, 2 passed in 0.30s =============================
    
    VERDICT: RETRY_NEEDED
    FEEDBACK:
    - test_division_by_zero failed with ZeroDivisionError
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    assert res.tests_executed is True
    assert res.tests_passed is False
    assert res.verdict == VerificationVerdict.RETRY_NEEDED
    assert "test_division_by_zero failed" in res.issues_found[0]


def test_parse_verdict_retry_needed_with_bullets():
    verifier = CodexVerifier()
    raw = """
    Анализ показал следующие недочёты:
    
    VERDICT: RETRY_NEEDED
    FEEDBACK:
    - Не обработан случай деления на ноль в calculator.py
    - Отсутствует валидация отрицательных чисел
    """
    res = verifier.parse_verification_output(raw, exit_code=0)
    assert res.verdict == VerificationVerdict.RETRY_NEEDED
    assert len(res.issues_found) == 2
    assert "Не обработан случай деления на ноль" in res.issues_found[0]
    assert "Отсутствует валидация отрицательных чисел" in res.issues_found[1]


def test_parse_verdict_explicit_failed():
    verifier = CodexVerifier()
    raw = """
    Критическая ошибка: код нарушает архитектурные ограничения безопасности.
    
    VERDICT: FAILED
    FEEDBACK:
    - Нарушение изоляции песочницы.
    """
    res = verifier.parse_verification_output(raw, exit_code=1)
    assert res.verdict == VerificationVerdict.FAILED
    assert res.exit_code == 1


def test_capture_git_diff_untracked_files(tmp_path: Path):
    """Test capture_git_diff includes contents of new untracked files."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Init git repo
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), capture_output=True, check=True)

    # Initial commit
    init_file = repo_dir / "init.txt"
    init_file.write_text("initial content", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), capture_output=True, check=True)

    # Create new untracked file
    new_file = repo_dir / "new_feature.py"
    new_file.write_text("def new_feature():\n    return 42\n", encoding="utf-8")

    verifier = CodexVerifier()
    diff = verifier.capture_git_diff(str(repo_dir))

    assert "new_feature.py" in diff
    assert "def new_feature():" in diff


def test_verify_custom_timeout(monkeypatch):
    verifier = CodexVerifier(timeout_sec=300.0)
    monkeypatch.setattr(verifier, "capture_git_diff", lambda cwd: "")
    captured_timeout = []

    def mock_run(cmd, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        class MockProc:
            stdout = "VERDICT: PASSED\nFEEDBACK:\n- OK"
            stderr = ""
            returncode = 0
        return MockProc()

    monkeypatch.setattr(subprocess, "run", mock_run)
    verifier.verify(
        original_prompt="test",
        acceptance_criteria="test",
        antigravity_result="done",
        cwd=".",
        timeout_sec=45.0,
    )
    assert captured_timeout == [45.0]

    captured_timeout.clear()
    verifier.verify(
        original_prompt="test",
        acceptance_criteria="test",
        antigravity_result="done",
        cwd=".",
        timeout_sec=500.0,
    )
    assert captured_timeout == [300.0]

