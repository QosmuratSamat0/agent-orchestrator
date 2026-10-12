from __future__ import annotations

import subprocess
import json
import os
import sys
import time
from pathlib import Path
import pytest
from agent_orchestrator.codex_verifier import CodexVerifier
from agent_orchestrator.models import VerificationVerdict


def event(command="python -m pytest -q", exit_code=0, output="5 passed in 0.20s"):
    return json.dumps({"type": "item.completed", "item": {"command": command,
                       "exit_code": exit_code, "aggregated_output": output}})


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
    res = verifier.parse_verification_output(raw, exit_code=0, events_jsonl=event())
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
    res = verifier.parse_verification_output(raw, exit_code=0, events_jsonl=event(exit_code=1, output="1 failed, 3 passed"))
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
    assert res_req.verdict == VerificationVerdict.FAILED
    assert any("не подтверждён событиями" in issue for issue in res_req.issues_found)

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
    assert res.verdict == VerificationVerdict.FAILED
    assert any("не подтверждён событиями" in issue for issue in res.issues_found)


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
    res = verifier.parse_verification_output(raw, exit_code=0, events_jsonl=event(output="4 passed in 0.45s"))
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
    res = verifier.parse_verification_output(raw, exit_code=0, events_jsonl=event(exit_code=1, output="1 failed, 2 passed"))
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


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.py"
    script.write_text('''import json, os, pathlib, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
args = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_ROOT"])
root.joinpath("argv.json").write_text(json.dumps(args), encoding="utf-8")
root.joinpath("stdin.txt").write_text(sys.stdin.buffer.read().decode("utf-8"), encoding="utf-8")
if os.environ.get("FAKE_CHILD"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    root.joinpath("child.pid").write_text(str(child.pid), encoding="ascii")
    root.joinpath("parent.pid").write_text(str(os.getpid()), encoding="ascii")
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if os.environ.get("FAKE_CHANGE"):
    pathlib.Path(args[args.index("-C") + 1], os.environ["FAKE_CHANGE"]).write_text("changed", encoding="utf-8")
if os.environ.get("FAKE_CACHES"):
    cwd = pathlib.Path(args[args.index("-C") + 1])
    for name in ("__pycache__", ".pytest_cache"):
        (cwd / name).mkdir(exist_ok=True)
        (cwd / name / "cache.pyc").write_text("cache", encoding="utf-8")
if "FAKE_FINAL" in os.environ:
    pathlib.Path(args[args.index("-o") + 1]).write_text(os.environ["FAKE_FINAL"], encoding="utf-8")
print(os.environ.get("FAKE_EVENTS", ""), flush=True)
print(os.environ.get("FAKE_STDERR", ""), file=sys.stderr, flush=True)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
''', encoding="utf-8")
    monkeypatch.setenv("FAKE_ROOT", str(tmp_path))
    monkeypatch.setenv("FAKE_FINAL", "VERDICT: PASSED\nFEEDBACK:\n- Готово")
    monkeypatch.setenv("FAKE_EVENTS", "")
    return CodexVerifier(codex_bin=[sys.executable, str(script)]), tmp_path


def test_fake_invocation_stdin_large_prompt_and_result_cap(fake_codex):
    verifier, root = fake_codex
    result = verifier.verify("unique prompt " + "x" * 45000, "", "z" * 25000, str(root))
    args = json.loads((root / "argv.json").read_text(encoding="utf-8"))
    stdin = (root / "stdin.txt").read_text(encoding="utf-8")
    assert result.verdict == VerificationVerdict.PASSED
    assert args[0:2] == ["exec", "--json"]
    assert "-o" in args and args[-1] == "-"
    assert "unique prompt" not in " ".join(args)
    assert "unique prompt" in stdin and "последние 20000" in stdin
    assert "z" * 20000 in stdin and "z" * 20001 not in stdin
    assert not list(root.glob("final.txt"))


def test_final_message_only_and_last_lines(fake_codex, monkeypatch):
    verifier, root = fake_codex
    monkeypatch.setenv("FAKE_FINAL", "VERDICT: PASSED\nTESTS_STATUS: FAILED\nVERDICT: RETRY_NEEDED\nTESTS_STATUS: PASSED")
    monkeypatch.setenv("FAKE_EVENTS", json.dumps({"text": "VERDICT: PASSED"}))
    monkeypatch.setenv("FAKE_STDERR", "VERDICT: PASSED")
    result = verifier.verify("edit docs", "", "", str(root))
    assert result.verdict == VerificationVerdict.RETRY_NEEDED
    assert not result.tests_passed
    assert any("TESTS_STATUS" in issue for issue in result.issues_found)


@pytest.mark.parametrize("final,code", [(None, 0), ("", 0), ("VERDICT: RETRY_NEEDED", 1)])
def test_missing_final_or_crash_is_failed(fake_codex, monkeypatch, final, code):
    verifier, root = fake_codex
    if final is None:
        monkeypatch.delenv("FAKE_FINAL")
    else:
        monkeypatch.setenv("FAKE_FINAL", final)
    monkeypatch.setenv("FAKE_EXIT", str(code))
    result = verifier.verify("edit docs", "", "", str(root))
    assert result.verdict == VerificationVerdict.FAILED
    if final is None or final == "":
        assert any("не вернул итоговый ответ" in issue for issue in result.issues_found)


def test_evidence_last_run_and_untrusted_text():
    verifier = CodexVerifier()
    final = "5 passed\nTESTS_STATUS: PASSED\nVERDICT: PASSED"
    failed = event(exit_code=1, output="2 failed")
    passed = event(output="5 passed")
    assert verifier.parse_verification_output(final, 0, prompt="write tests", events_jsonl=failed).verdict == VerificationVerdict.RETRY_NEEDED
    result = verifier.parse_verification_output(final, 0, prompt="write tests", events_jsonl=failed + "\n" + passed)
    assert result.verdict == VerificationVerdict.PASSED and result.tests_passed
    assert "python -m pytest" in result.test_evidence
    no_events = verifier.parse_verification_output(final, 0, prompt="write tests")
    assert no_events.verdict == VerificationVerdict.FAILED and not no_events.tests_executed
    assert verifier.parse_verification_output(final, 0, prompt="edit docs", events_jsonl="garbage\n{").verdict == VerificationVerdict.PASSED


@pytest.mark.parametrize("command,output", [
    ("python -m pytest", "WARNING retry: connection failed, retrying\n5 passed in 0.12s"),
    ("python -m pytest", "tests/test_x.py::test_login_failed_attempt PASSED\n5 passed in 0.12s"),
    ("python -m pytest", "5 passed, 0 failed in 0.1s"),
    ("python -m pytest", "==== 5 passed, 1 warning in 0.3s ===="),
    ("python -m unittest", "Ran 3 tests in 0.001s\n\nOK"),
])
def test_successful_summary_ignores_incidental_failures(command, output):
    result = CodexVerifier().parse_verification_output(
        "VERDICT: PASSED", 0, prompt="write tests",
        events_jsonl=event(command=command, output=output),
    )
    assert result.verdict == VerificationVerdict.PASSED
    assert result.tests_executed and result.tests_passed
    assert not any("проваленные тесты" in issue for issue in result.issues_found)


@pytest.mark.parametrize("command,code,output", [
    ("python -m pytest", 1, "5 passed"),
    ("python -m pytest", 0, "==== 1 failed, 4 passed in 0.2s ===="),
    ("python -m unittest", 0, "Ran 3 tests in 0.001s\nFAILED (failures=1)"),
])
def test_failed_summary_or_exit_code_overrides_passed(command, code, output):
    result = CodexVerifier().parse_verification_output(
        "VERDICT: PASSED", 0, prompt="write tests",
        events_jsonl=event(command=command, exit_code=code, output=output),
    )
    assert result.verdict == VerificationVerdict.RETRY_NEEDED
    assert result.tests_executed and not result.tests_passed


def test_executed_without_summary_is_not_confirmed():
    result = CodexVerifier().parse_verification_output(
        "VERDICT: PASSED", 0, prompt="write tests",
        events_jsonl=event(output="tests/test_x.py::test_example PASSED"),
    )
    assert result.verdict == VerificationVerdict.FAILED
    assert result.tests_executed and not result.tests_passed
    assert "нет итоговой строки" in result.test_evidence


@pytest.mark.parametrize("prompt,required", [
    ("use the latest contest attestation", False), ("добавь тесты", True),
    ("write tests", True), ("проверить покрытие", True),
])
def test_required_word_boundaries(prompt, required):
    result = CodexVerifier().parse_verification_output("VERDICT: PASSED", 0, prompt=prompt)
    assert (result.verdict == VerificationVerdict.FAILED) is required


def test_unrecognized_json_and_recursive_command():
    verifier = CodexVerifier()
    result = verifier.parse_verification_output("VERDICT: PASSED", 0, events_jsonl='{"event": "done"}')
    assert any("Формат событий" in issue for issue in result.issues_found)
    nested = json.dumps({"items": [{"cmd": ["python", "-m", "pytest"], "exitCode": 0, "stdout": "5 passed"}]})
    result = verifier.parse_verification_output("VERDICT: PASSED", 0, events_jsonl=nested)
    assert result.tests_executed and result.tests_passed


def _git_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    (path / "tracked.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


def test_workspace_change_and_cache_exception(fake_codex, monkeypatch):
    verifier, root = fake_codex
    repo = root / "repo"
    repo.mkdir()
    _git_repo(repo)
    monkeypatch.setenv("FAKE_CHANGE", "tracked.txt")
    result = verifier.verify("edit docs", "", "", str(repo))
    assert result.verdict == VerificationVerdict.FAILED
    assert "Верификатор изменил файлы проекта." in result.issues_found
    monkeypatch.delenv("FAKE_CHANGE")
    monkeypatch.setenv("FAKE_CACHES", "1")
    result = verifier.verify("edit docs", "", "", str(repo))
    assert result.verdict == VerificationVerdict.PASSED
    result = verifier.verify("edit docs", "", "", str(root))
    assert result.verdict == VerificationVerdict.PASSED
    assert any("Git-репозиторием" in issue for issue in result.issues_found)


def test_fingerprint_tracks_untracked_content_and_ignores_caches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    new_file = repo / "new_file.py"
    new_file.write_text("first", encoding="utf-8")
    before = CodexVerifier._workspace_fingerprint(str(repo))
    new_file.write_text("other", encoding="utf-8")
    after = CodexVerifier._workspace_fingerprint(str(repo))
    assert before != after
    for name in ("__pycache__", ".pytest_cache"):
        folder = repo / name
        folder.mkdir()
        (folder / "cache.pyc").write_bytes(b"cache")
    assert CodexVerifier._workspace_fingerprint(str(repo)) == after


def test_fingerprint_without_commits_tracks_untracked_content(tmp_path):
    repo = tmp_path / "repo_without_commits"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    new_file = repo / "new_file.py"
    new_file.write_text("first", encoding="utf-8")
    before = CodexVerifier._workspace_fingerprint(str(repo))
    assert before is not None
    new_file.write_text("other", encoding="utf-8")
    assert CodexVerifier._workspace_fingerprint(str(repo)) != before


def test_timeout_kills_own_process_tree(fake_codex, monkeypatch):
    if os.name == "nt":
        probe = subprocess.run(["tasklist", "/FI", "PID eq 0"], capture_output=True, text=True)
        if probe.returncode != 0 and "Access denied" in (probe.stdout + probe.stderr):
            pytest.skip("Среда запрещает tasklist/taskkill; завершение дерева нельзя проверить")
    verifier, root = fake_codex
    monkeypatch.setenv("FAKE_CHILD", "1")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    start = time.monotonic()
    result = verifier.verify("edit docs", "", "", str(root), timeout_sec=2)
    assert result.verdict == VerificationVerdict.FAILED and result.exit_code == 124
    assert time.monotonic() - start < 15
    for filename in ("parent.pid", "child.pid"):
        pid = int((root / filename).read_text(encoding="ascii"))
        if os.name == "nt":
            listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
            assert str(pid) not in listing.stdout
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_timeout_without_child_returns_124(fake_codex, monkeypatch):
    verifier, root = fake_codex
    monkeypatch.setenv("FAKE_SLEEP", "60")
    result = verifier.verify("edit docs", "", "", str(root), timeout_sec=1)
    assert result.verdict == VerificationVerdict.FAILED and result.exit_code == 124

