from __future__ import annotations

import logging
import os
import re
import subprocess
import hashlib
import json
import signal
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

from agent_orchestrator.models import VerificationResult, VerificationVerdict

logger = logging.getLogger("agent_orchestrator.codex_verifier")

_NOISE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}

# One entry per test stack. Only the LAST run of each stack counts, so a retried command replaces a failed one,
# while a repository with several stacks (Go backend + JS frontend) has to be green in every stack that was run.
_RUNNER_KINDS = (
    ("pytest", re.compile(r"\b(?:pytest|py\.test)\b", re.I)),
    ("unittest", re.compile(r"\bunittest\b", re.I)),
    ("go", re.compile(r"\bgo\s+test\b", re.I)),
    ("js", re.compile(r"\b(?:npm\s+(?:test|run\s+test)|yarn\s+test|pnpm\s+test|vitest|jest|node\s+--test)\b", re.I)),
    ("cargo", re.compile(r"\bcargo\s+test\b", re.I)),
    ("dotnet", re.compile(r"\bdotnet\s+test\b", re.I)),
    ("maven", re.compile(r"\bmvn\s+test\b", re.I)),
    ("gradle", re.compile(r"\bgradle\s+test\b", re.I)),
    ("tox", re.compile(r"\b(?:tox|nox)\b", re.I)),
)


def _is_noise_path(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return any(part in _NOISE_DIRS for part in parts) or name.endswith(".pyc")


class CodexVerifier:
    """Executes a separate Codex process to independently verify results and run tests."""

    def __init__(
        self,
        codex_bin: str | Sequence[str] = r"C:\Users\admin\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe",
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
            # Bytes + explicit UTF-8: text=True decoded with the console code page, and a single undecodable
            # byte (Cyrillic/Kazakh sources) left stdout as None -> "'NoneType' object has no attribute 'strip'".
            status_res = subprocess.run(
                ["git", "-c", "core.quotepath=false", "status", "--porcelain", "-z", "--untracked-files=all"],
                stdin=subprocess.DEVNULL,
                cwd=cwd_path,
                capture_output=True,
                timeout=15,
            )
            if status_res.returncode != 0:
                return "Not a git repository or git status failed."

            entries = [e for e in status_res.stdout.decode("utf-8", errors="replace").split("\0") if e]
            if not entries:
                return "No changes in working tree (git status is clean)."

            diff_parts: List[str] = []

            # 1. Tracked modified diff
            diff_res = subprocess.run(
                ["git", "-c", "core.quotepath=false", "diff", "HEAD"],
                stdin=subprocess.DEVNULL,
                cwd=cwd_path,
                capture_output=True,
                timeout=30,
            )
            diff_text = diff_res.stdout.decode("utf-8", errors="replace")
            if diff_text.strip():
                diff_parts.append(diff_text)

            # 2. Untracked new files (marked with '??')
            for entry in entries:
                if not entry.startswith("?? "):
                    continue
                rel_path = entry[3:]
                if _is_noise_path(rel_path):
                    continue
                file_full = Path(cwd_path) / rel_path
                if file_full.is_file():
                    try:
                        content = file_full.read_text(encoding="utf-8", errors="replace")
                        if len(content) > 20000:
                            content = content[:20000] + "\n[файл сокращён]"
                        diff_parts.append(
                            f"--- /dev/null\n+++ b/{rel_path} (New untracked file)\n{content}"
                        )
                    except Exception as fe:
                        diff_parts.append(
                            f"New untracked file {rel_path} (read error: {fe})"
                        )

            return "\n\n".join(diff_parts) if diff_parts else "Changed files:\n" + "\n".join(entries)
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
        if len(antigravity_result) > 20000:
            antigravity_result = "[Отчёт Antigravity сокращён; показаны последние 20000 символов.]\n" + antigravity_result[-20000:]
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
Не изменяй файлы проекта: только читай их и запускай тесты.
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
        effective_timeout = min(self.timeout_sec, timeout_sec) if timeout_sec is not None else self.timeout_sec
        before = self._workspace_fingerprint(cwd)
        prompt = self.build_verification_prompt(
            original_prompt, acceptance_criteria, antigravity_result,
            self.capture_git_diff(cwd), attempt,
        )
        prefix = [self.codex_bin] if isinstance(self.codex_bin, str) else list(self.codex_bin)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                final_file = Path(temp_dir) / "final.txt"
                cmd = prefix + ["exec", "--json", "-o", str(final_file)]
                if self.skip_git_repo_check:
                    cmd.append("--skip-git-repo-check")
                if self.ephemeral:
                    cmd.append("--ephemeral")
                if self.sandbox_mode:
                    cmd.extend(["-s", self.sandbox_mode])
                if cwd and os.path.exists(cwd):
                    cmd.extend(["-C", str(cwd)])
                cmd.append("-")
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", **options,
                )
                try:
                    stdout, _stderr = proc.communicate(input=prompt, timeout=effective_timeout)
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(proc.pid)
                    try:
                        proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            logger.warning("Каналы процесса %s не закрылись после остановки", proc.pid)
                    return VerificationResult(
                        verdict=VerificationVerdict.FAILED,
                        feedback=f"Верификатор превысил лимит времени {effective_timeout} с.",
                        exit_code=124, raw_output="TimeoutExpired",
                        issues_found=["Превышен лимит времени проверки."],
                    )
                final_message = final_file.read_text(encoding="utf-8", errors="replace") if final_file.is_file() else ""
            after = self._workspace_fingerprint(cwd)
            result = self.parse_verification_output(
                final_message, proc.returncode, acceptance_criteria, original_prompt,
                stdout or "", before is not None and after is not None and before != after,
            )
            if before is None or after is None:
                result.issues_found.append("Проверка изменений пропущена: каталог не является Git-репозиторием или Git недоступен.")
            return result
        except Exception as exc:
            logger.exception("Не удалось выполнить верификатор")
            return VerificationResult(
                verdict=VerificationVerdict.FAILED,
                feedback=f"Не удалось запустить верификатор: {exc}",
                exit_code=1, raw_output=str(exc),
                issues_found=[f"Ошибка запуска верификатора: {exc}"],
            )

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
            else:
                os.killpg(pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Не удалось завершить дерево процесса %s: %s", pid, exc)

    @staticmethod
    def _workspace_fingerprint(cwd: str) -> str | None:
        try:
            repo = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            if repo.returncode != 0 or repo.stdout.strip() != b"true":
                return None
            head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=cwd,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            diff_command = ["git", "diff", "HEAD"] if head.returncode == 0 else ["git", "diff"]
            diff = subprocess.run(diff_command, cwd=cwd,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True).stdout
            status = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                                    cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True).stdout
            untracked = []
            for entry in status.split(b"\0"):
                if not entry.startswith(b"?? "):
                    continue
                name = entry[3:].decode("utf-8", errors="replace").replace("\\", "/")
                if _is_noise_path(name):
                    continue
                untracked.append(name)
            digest = hashlib.sha256(diff + b"\0")
            for name in sorted(untracked):
                file_path = Path(cwd) / name
                digest.update(name.encode("utf-8") + b"\0")
                if file_path.is_symlink():
                    digest.update(os.readlink(file_path).encode("utf-8", errors="replace"))
                    continue
                size = file_path.stat().st_size
                if size > 5 * 1024 * 1024:
                    stat = file_path.stat()
                    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
                else:
                    digest.update(hashlib.sha256(file_path.read_bytes()).digest())
            return digest.hexdigest()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("Не удалось проверить изменения Git: %s", exc)
            return None

    @staticmethod
    def _test_evidence(events_jsonl: str) -> tuple[bool, bool, bool, str, bool]:
        commands = []
        json_seen = False

        def visit(value: object) -> None:
            if isinstance(value, dict):
                command = value.get("command", value.get("cmd"))
                code = value.get("exit_code", value.get("exitCode"))
                if isinstance(command, (str, list)) and code is not None:
                    command_text = " ".join(map(str, command)) if isinstance(command, list) else command
                    output = value.get("aggregated_output", value.get("output", value.get("formatted_output", "")))
                    output = str(output or "")
                    if value.get("stdout") is not None:
                        output = "\n".join(filter(None, [output, str(value["stdout"]), str(value.get("stderr", ""))]))
                    commands.append((command_text, code, output))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for line in events_jsonl.splitlines():
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                json_seen = True
                visit(value)

        last_by_kind: dict = {}
        for command_text, code, output in commands:
            kind = next((name for name, rx in _RUNNER_KINDS if rx.search(command_text)), None)
            if kind:
                last_by_kind[kind] = (command_text, code, output)
        if not last_by_kind:
            return False, False, False, "", json_seen and not commands
        any_failed = False
        all_passed = True
        parts = []
        for kind, (command, code, output) in last_by_kind.items():
            summary, ok, summary_failed = CodexVerifier._run_outcome(kind, output)
            run_failed = str(code) != "0" or summary_failed
            run_passed = str(code) == "0" and ok and not run_failed
            any_failed = any_failed or run_failed
            all_passed = all_passed and run_passed
            parts.append(f"{command[:200]} (код {code}): {(summary or 'нет итоговой строки')[:300]}")
        passed = all_passed and not any_failed
        return True, passed, any_failed, " | ".join(parts), json_seen and not commands

    @staticmethod
    def _run_outcome(kind: str, output: str) -> tuple[str, bool, bool]:
        """(summary line, passed according to the runner's own summary, summary reports failures)."""
        lines = [line.strip() for line in output.splitlines()]
        if kind == "pytest":
            summaries = [line.strip("= ") for line in lines
                         if re.match(r"^\s*=*\s*\d+\s+(?:passed|failed|errors?)\b", line, re.I)]
            if not summaries:
                return "", False, False
            counts = {k.lower(): int(n) for n, k in re.findall(r"\b(\d+)\s+(passed|failed|errors?)\b", summaries[-1], re.I)}
            bad = counts.get("failed", 0) > 0 or counts.get("error", 0) > 0 or counts.get("errors", 0) > 0
            return summaries[-1], counts.get("passed", 0) > 0 and not bad, bad
        if kind == "unittest":
            summaries = [line for line in lines if re.fullmatch(r"OK(?:\s*\([^)]*\))?|FAILED\s*\([^)]*\)", line, re.I)]
            if not summaries:
                return "", False, False
            ran = re.findall(r"^Ran\s+(\d+)\s+tests?\b", output, re.I | re.M)
            bad = bool(re.match(r"FAILED\b", summaries[-1], re.I))
            ok = bool(re.match(r"OK\b", summaries[-1], re.I)) and (not ran or int(ran[-1]) > 0)
            return summaries[-1], ok, bad
        if kind == "go":
            # `go test ./...` prints one line per package and never a total.
            ok_lines = [line for line in lines if re.match(r"^ok\s+\S+", line)]
            bad_lines = [line for line in lines
                         if re.match(r"^(?:FAIL\b|--- FAIL:|panic:)", line)
                         or "[build failed]" in line or "[setup failed]" in line]
            if not ok_lines and not bad_lines:
                return "", False, False
            summary = f"go test: пакетов ok — {len(ok_lines)}, ошибок — {len(bad_lines)}"
            return summary, bool(ok_lines) and not bad_lines, bool(bad_lines)
        # node:test (`# pass N` / `ℹ pass N`), then Jest/Vitest/generic summaries.
        node_pass = re.findall(r"^(?:#|ℹ)\s*pass\s+(\d+)", output, re.I | re.M)
        node_fail = re.findall(r"^(?:#|ℹ)\s*fail\s+(\d+)", output, re.I | re.M)
        if node_pass:
            failed_n = int(node_fail[-1]) if node_fail else 0
            return f"pass {node_pass[-1]}, fail {failed_n}", int(node_pass[-1]) > 0 and failed_n == 0, failed_n > 0
        summaries = [line.strip("= ") for line in lines
                     if re.match(r"^\s*=*\s*(?:tests?:?\s+)?(?:\d+\s+passed|passed\b)", line, re.I)]
        if not summaries:
            return "", False, False
        bad = any(int(n) > 0 for n in re.findall(r"\b(\d+)\s+(?:failed|errors?)\b", summaries[-1], re.I))
        return summaries[-1], not bad, bad

    def parse_verification_output(
        self,
        final_message: str,
        exit_code: int,
        criteria: str = "",
        prompt: str = "",
        events_jsonl: str = "",
        workspace_changed: bool = False,
    ) -> VerificationResult:
        raw_output = final_message + "\n--- events ---\n" + events_jsonl[-50000:]
        issues: List[str] = []
        verdicts = re.findall(r"^\s*VERDICT:\s*(PASSED|RETRY_NEEDED|FAILED)\s*$", final_message, re.I | re.M)
        statuses = re.findall(r"^\s*TESTS_STATUS:\s*(PASSED|FAILED|NOT_RUN)\s*$", final_message, re.I | re.M)
        feedback_match = re.search(r"^\s*FEEDBACK:\s*([\s\S]*)", final_message, re.I | re.M)
        feedback = feedback_match.group(1).strip() if feedback_match else final_message[-2000:].strip()
        if feedback_match:
            for line in feedback.splitlines():
                cleaned = re.sub(r"^\s*(?:[-*•]|\d+\.)\s*", "", line)
                if cleaned != line and cleaned:
                    issues.append(cleaned)
        executed, passed, failed, evidence, unrecognized = self._test_evidence(events_jsonl)
        if unrecognized:
            issues.append("Формат событий верификатора не распознан: команды не найдены.")
        if statuses and statuses[-1].upper() == "PASSED" and not passed:
            issues.append("Заявлен TESTS_STATUS: PASSED, но успешный запуск тестов не подтверждён событиями верификатора.")
        tests_required = bool(re.search(
            r"\b(?:test|tests|testing|pytest|unittest|coverage)\b|\b(?:тест\w*|покрыти\w*)\b",
            f"{criteria} {prompt}", re.I,
        ))
        verdict = VerificationVerdict(verdicts[-1].lower()) if verdicts else VerificationVerdict.FAILED
        if not verdicts:
            issues.append("Отсутствует явный валидный вердикт (VERDICT: PASSED | RETRY_NEEDED | FAILED).")
            if not final_message.strip():
                issues.append("Верификатор не вернул итоговый ответ")
        if exit_code != 0:
            verdict = VerificationVerdict.FAILED
            issues.append(f"Процесс верификатора завершился с кодом ошибки {exit_code}.")
        if workspace_changed:
            verdict = VerificationVerdict.FAILED
            issues.append("Верификатор изменил файлы проекта.")
        if failed and verdict == VerificationVerdict.PASSED:
            verdict = VerificationVerdict.RETRY_NEEDED
            issues.append("Зафиксированы проваленные тесты. Вердикт переведён в RETRY_NEEDED.")
        if tests_required and not passed and not failed:
            if verdict == VerificationVerdict.PASSED:
                verdict = VerificationVerdict.FAILED
                issues.append("Тесты требуются, но успешный запуск не подтверждён событиями верификатора.")
            elif verdict == VerificationVerdict.RETRY_NEEDED:
                # Nothing is approved here and the findings are fixable: keep the retry loop alive.
                issues.append(
                    "Тесты требуются, но успешный запуск не подтверждён событиями верификатора; "
                    "замечания переданы на исправление."
                )
        return VerificationResult(
            verdict=verdict, feedback=feedback, exit_code=exit_code, raw_output=raw_output,
            issues_found=issues, tests_executed=executed, tests_passed=passed,
            test_evidence=evidence,
        )
