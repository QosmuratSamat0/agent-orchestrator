from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from agent_orchestrator.models import VerificationResult, VerificationVerdict

logger = logging.getLogger("agent_orchestrator.codex_verifier")


class CodexVerifier:
    """Executes a separate Codex process to independently verify results and run tests."""

    def __init__(
        self,
        codex_bin: str = r"C:\Users\admin\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe",
        timeout_sec: float = 300.0,
        sandbox_mode: str = "workspace-write",
        skip_git_repo_check: bool = True,
        ephemeral: bool = True,
    ):
        self.codex_bin = codex_bin
        self.timeout_sec = timeout_sec
        self.sandbox_mode = sandbox_mode
        self.skip_git_repo_check = skip_git_repo_check
        self.ephemeral = ephemeral

    def capture_git_diff(self, cwd: str | Path) -> str:
        """Captures git diff of the workspace to inspect actual changes, including untracked files."""
        cwd_path = str(cwd)
        try:
            # Check git status
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if status_res.returncode != 0:
                return "Not a git repository or git status failed."

            if not status_res.stdout.strip():
                return "No changes in working tree (git status is clean)."

            diff_parts: List[str] = []

            # 1. Tracked modified diff
            diff_res = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=cwd_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if diff_res.stdout.strip():
                diff_parts.append(diff_res.stdout)

            # 2. Untracked new files (marked with '??')
            for line in status_res.stdout.splitlines():
                line = line.strip()
                if line.startswith("??"):
                    rel_path = line[2:].strip().strip('"')
                    file_full = Path(cwd_path) / rel_path
                    if file_full.is_file():
                        try:
                            content = file_full.read_text(encoding="utf-8", errors="replace")
                            diff_parts.append(
                                f"--- /dev/null\n+++ b/{rel_path} (New untracked file)\n{content}"
                            )
                        except Exception as fe:
                            diff_parts.append(
                                f"New untracked file {rel_path} (read error: {fe})"
                            )

            return "\n\n".join(diff_parts) if diff_parts else f"Changed files:\n{status_res.stdout}"
        except Exception as e:
            logger.warning(f"Failed to capture git diff: {e}")
            return f"Git diff capture failed: {e}"

    def build_verification_prompt(
        self,
        original_prompt: str,
        acceptance_criteria: str,
        antigravity_result: str,
        git_diff: str,
        attempt: int = 1,
    ) -> str:
        return f"""Ты — автоматический верификатор качества кода (Codex Verifier).
Твоя задача — объективно проверить работу, выполненную агентом Antigravity (попытка {attempt}), выполнить валидацию изменений и вынести вердикт.

=== 1. ИСХОДНОЕ ЗАДАНИЕ ===
{original_prompt}

=== 2. КРИТЕРИИ ПРИЁМКИ ===
{acceptance_criteria or 'Специфические критерии не указаны. Оценивай общее качество и работоспособность.'}

=== 3. ОТЧЁТ ANTIGRAVITY О ВЫПОЛНЕНИИ ===
{antigravity_result}

=== 4. ИЗМЕНЕНИЯ В КОДЕ (GIT DIFF) ===
{git_diff[:25000]}

=== ИНСТРУКЦИЯ ПО ВЕРИФИКАЦИИ ===
1. Изучи изменения на соответствие исходному заданию и критериям приёмки.
2. Проверь наличие синтаксических ошибок, регрессий или пропущенных требований.
3. Применимо к репозиторию запусти тесты (например, pytest или unittest) и зафиксируй их результат.
4. В самом конце твоего ответа ОБЯЗАТЕЛЬНО выведи структурированный блок вердикта:

TESTS_STATUS: <PASSED | FAILED | NOT_RUN>
VERDICT: <PASSED | RETRY_NEEDED | FAILED>
FEEDBACK:
- <конкретное замечание 1>
- <конкретное замечание 2>

Правила для вердикта:
- VERDICT: PASSED допустим ТОЛЬКО если код полностью соответствует критериям, не содержит ошибок и все запущенные тесты успешно пройдены (TESTS_STATUS: PASSED с логами тестового раннера или NOT_RUN, если тесты не предусмотрены заданием).
- Если тесты провалены (TESTS_STATUS: FAILED) или найдены исправимые недочёты — укажи VERDICT: RETRY_NEEDED и перечисли замечания в блоке FEEDBACK.
- Если произошёл критический сбой, нарушение песочницы или архитектурный тупик — укажи VERDICT: FAILED.
"""

    def verify(
        self,
        original_prompt: str,
        acceptance_criteria: str,
        antigravity_result: str,
        cwd: str,
        attempt: int = 1,
        timeout_sec: Optional[float] = None,
    ) -> VerificationResult:
        effective_timeout = (
            min(self.timeout_sec, timeout_sec)
            if timeout_sec is not None
            else self.timeout_sec
        )
        logger.info(
            f"Starting Codex verification for attempt {attempt} in {cwd} (timeout={effective_timeout}s)"
        )
        git_diff = self.capture_git_diff(cwd)
        verification_prompt = self.build_verification_prompt(
            original_prompt=original_prompt,
            acceptance_criteria=acceptance_criteria,
            antigravity_result=antigravity_result,
            git_diff=git_diff,
            attempt=attempt,
        )

        cmd: List[str] = [self.codex_bin, "exec"]
        if self.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        if self.ephemeral:
            cmd.append("--ephemeral")
        if self.sandbox_mode:
            cmd.extend(["-s", self.sandbox_mode])
        if cwd and os.path.exists(cwd):
            cmd.extend(["-C", str(cwd)])

        cmd.append(verification_prompt)

        try:
            logger.debug(f"Running Codex command: {' '.join(cmd[:5])} ...")
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
            )
            raw_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            exit_code = proc.returncode

            logger.info(
                f"Codex verifier process exited with code {exit_code} (output length: {len(raw_output)})"
            )
            return self.parse_verification_output(
                raw_output=raw_output,
                exit_code=exit_code,
                criteria=acceptance_criteria,
                prompt=original_prompt,
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Codex verifier timed out after {effective_timeout}s")
            return VerificationResult(
                verdict=VerificationVerdict.FAILED,
                feedback=f"Codex verifier execution timed out after {effective_timeout} seconds.",
                exit_code=124,
                raw_output="TimeoutExpired",
                issues_found=["Verification process timed out."],
                tests_executed=False,
                tests_passed=False,
            )
        except Exception as e:
            logger.error(f"Failed to execute Codex verifier: {e}")
            return VerificationResult(
                verdict=VerificationVerdict.FAILED,
                feedback=f"Failed to spawn Codex verifier: {e}",
                exit_code=1,
                raw_output=str(e),
                issues_found=[f"Execution exception: {e}"],
                tests_executed=False,
                tests_passed=False,
            )

    def parse_verification_output(
        self,
        raw_output: str,
        exit_code: int,
        criteria: str = "",
        prompt: str = "",
    ) -> VerificationResult:
        """Extracts structured verdict and actionable feedback from Codex output with strict quality checks."""
        # 1. Search for explicit structured VERDICT: <PASSED|RETRY_NEEDED|FAILED>
        verdict_match = re.search(
            r"^\s*VERDICT:\s*(PASSED|RETRY_NEEDED|FAILED)\s*$",
            raw_output,
            re.MULTILINE | re.IGNORECASE,
        )

        # 2. Extract FEEDBACK block and individual bullet points
        feedback = ""
        issues: List[str] = []
        feedback_match = re.search(
            r"FEEDBACK:\s*(.*)", raw_output, re.IGNORECASE | re.DOTALL
        )
        if feedback_match:
            feedback = feedback_match.group(1).strip()
            for line in feedback.splitlines():
                line = line.strip()
                if line.startswith(("-", "*", "•")) or re.match(r"^\d+\.", line):
                    cleaned = re.sub(r"^[-*•\d.]\s*", "", line).strip()
                    if cleaned:
                        issues.append(cleaned)
        else:
            feedback = raw_output[-2000:].strip() if raw_output else ""

        # 3. Determine if tests were explicitly required by criteria or prompt
        full_req = f"{criteria} {prompt}".lower()
        tests_required = any(
            kw in full_req for kw in ["тест", "test", "pytest", "unittest", "тестирован", "coverage"]
        )

        # 4. Test execution analysis (strict, NOT relying merely on textual claims)
        tests_executed = False
        tests_passed = False

        test_status_match = re.search(
            r"^\s*TESTS_STATUS:\s*(PASSED|FAILED|NOT_RUN)\s*$",
            raw_output,
            re.MULTILINE | re.IGNORECASE,
        )

        # Real test runner patterns
        has_runner_fail = bool(
            re.search(
                r"(=+\s*\d+\s+failed.*=+|\b\d+\s+failed\b|FAILED \(failures=\d+|FAILED \(errors=\d+|\bERRORS:\s*\d+)",
                raw_output,
                re.IGNORECASE,
            )
        )
        has_runner_pass = bool(
            re.search(
                r"(=+\s*\d+\s+passed.*=+|\b\d+\s+passed\b|(?:\n|^)OK\s*(?:\(.*\))?$)",
                raw_output,
                re.MULTILINE | re.IGNORECASE,
            )
        )

        if test_status_match:
            ts = test_status_match.group(1).upper()
            if ts == "PASSED":
                # Do NOT rely solely on model's textual statement! Require real runner confirmation.
                if has_runner_pass and not has_runner_fail:
                    tests_executed = True
                    tests_passed = True
                elif has_runner_fail:
                    tests_executed = True
                    tests_passed = False
                else:
                    # Model claimed TESTS_STATUS: PASSED, but NO runner output was found!
                    tests_executed = False
                    tests_passed = False
                    issues.append(
                        "Заявлен TESTS_STATUS: PASSED, однако в выводе отсутствуют подтверждения реального запуска тестового раннера (pytest/unittest)."
                    )
            elif ts == "FAILED":
                tests_executed = True
                tests_passed = False
            elif ts == "NOT_RUN":
                tests_executed = False
                tests_passed = False
        elif has_runner_fail:
            tests_executed = True
            tests_passed = False
        elif has_runner_pass:
            tests_executed = True
            tests_passed = True

        # 5. Determine initial verdict
        verdict: VerificationVerdict
        if not verdict_match:
            # STRICT REQUIREMENT: No explicit valid verdict MUST NEVER result in PASSED!
            verdict = VerificationVerdict.FAILED
            issues.append(
                "Отсутствует явный валидный вердикт (VERDICT: PASSED | RETRY_NEEDED | FAILED)."
            )
            if not feedback:
                feedback = "Верификатор не вернул явного валидного вердикта."
        else:
            parsed_v = verdict_match.group(1).upper()
            if parsed_v == "PASSED":
                verdict = VerificationVerdict.PASSED
            elif parsed_v == "RETRY_NEEDED":
                verdict = VerificationVerdict.RETRY_NEEDED
            else:
                verdict = VerificationVerdict.FAILED

        # 6. STRICT INTEGRITY CHECKS:
        # A) Non-zero exit code: Process failed or tool crashed. PASSED is FORBIDDEN.
        if exit_code != 0:
            if verdict == VerificationVerdict.PASSED:
                logger.warning(
                    f"Codex claimed PASSED in text, but process exited with non-zero code {exit_code}. Overriding to FAILED."
                )
                verdict = VerificationVerdict.FAILED
                issues.append(
                    f"Процесс верификатора завершился с кодом ошибки {exit_code} (PASSED аннулирован)."
                )
            elif verdict != VerificationVerdict.RETRY_NEEDED:
                verdict = VerificationVerdict.FAILED
                issues.append(f"Процесс верификатора завершился с кодом ошибки {exit_code}.")

        # B) Failed tests: If tests executed and failed, PASSED is FORBIDDEN.
        if tests_executed and not tests_passed:
            if verdict == VerificationVerdict.PASSED:
                logger.warning(
                    "Codex claimed PASSED in text, but tests failed. Overriding to RETRY_NEEDED."
                )
                verdict = VerificationVerdict.RETRY_NEEDED
                issues.append("Зафиксированы проваленные тесты. Вердикт переведён в RETRY_NEEDED.")

        # C) Tests required by criteria/prompt, but NOT confirmed as passed:
        if tests_required and not tests_passed:
            if verdict == VerificationVerdict.PASSED:
                logger.warning(
                    "Task criteria required tests, but tests are not confirmed passed. Overriding to RETRY_NEEDED."
                )
                verdict = VerificationVerdict.RETRY_NEEDED
                issues.append(
                    "Критерии задачи требуют успешных тестов, но их запуск не подтверждён логами раннера. Вердикт переведён в RETRY_NEEDED."
                )

        return VerificationResult(
            verdict=verdict,
            feedback=feedback,
            exit_code=exit_code,
            raw_output=raw_output,
            issues_found=issues,
            tests_executed=tests_executed,
            tests_passed=tests_passed,
        )
