from __future__ import annotations

import logging
from typing import List, Optional

from agent_orchestrator.models import (
    Task,
    TaskEvent,
    VerificationResult,
    VerificationVerdict,
)

logger = logging.getLogger("agent_orchestrator.retry_controller")


class RetryController:
    """Controls the correction loop and generates diagnostic reports."""

    def __init__(self):
        pass

    def can_retry(self, task: Task, verification: VerificationResult) -> bool:
        return verification.verdict == VerificationVerdict.RETRY_NEEDED

    def build_refinement_prompt(
        self,
        task: Task,
        verification: VerificationResult,
    ) -> str:
        iteration_num = task.retry_count + 2
        issues_formatted = ""
        if verification.issues_found:
            issues_formatted = "\n".join(f"- {issue}" for issue in verification.issues_found)
        else:
            issues_formatted = verification.feedback

        return f"""ВНИМАНИЕ: Это повторный шаг доработки (Итерация {iteration_num}).

ИСХОДНОЕ ЗАДАНИЕ:
{task.prompt}

КРИТЕРИИ ПРИЁМКИ:
{task.acceptance_criteria}

РЕЗУЛЬТАТ НЕЗАВИСИМОЙ ПРОВЕРКИ CODEX:
Проверка выявила замечания, требующие обязательного исправления:
{issues_formatted}

ПОЛНЫЙ ОТЗЫВ ВЕРИФИКАТОРА:
{verification.feedback}

ЗАДАЧА ДЛЯ ИСПРАВЛЕНИЯ:
1. Исправь исключительно указанные выше ошибки и замечания в кодовой базе.
2. Проверь работоспособность исправлений и отсутствие регрессий.
3. Сохрани все корректные изменения предыдущего шага.
"""

    def generate_final_report(
        self,
        task: Task,
        last_verification: Optional[VerificationResult] = None,
        events: Optional[List[TaskEvent]] = None,
    ) -> str:
        """Generates a comprehensive diagnostic report for team leads and auditors."""
        report_lines = [
            "# ИТОГОВЫЙ ОТЧЁТ ОРКЕСТРАТОРА (AGENT ORCHESTRATOR)",
            "",
            "## 1. Сводка задачи",
            f"- **ID задачи:** `{task.id}`",
            f"- **Название:** {task.title or 'Без названия'}",
            f"- **Итоговый статус:** `{task.status.value.upper()}`",
            f"- **Использованная модель Antigravity:** `{task.model}` (effort: `{task.effort}`)",
            f"- **Количество попыток / повторов:** {task.retry_count}",
            f"- **Рабочий каталог:** `{task.cwd}`",
            f"- **Время создания:** {task.created_at}",
            f"- **Время завершения:** {task.finished_at or 'В процессе'}",
            "",
            "## 2. Исходные требования",
            f"**Задание:**\n```\n{task.prompt}\n```",
            "",
            f"**Критерии приёмки:**\n```\n{task.acceptance_criteria or 'Стандартные критерии качества'}\n```",
            "",
            "## 3. Результаты верификации Codex",
        ]

        if last_verification:
            report_lines.extend([
                f"- **Вердикт:** `{last_verification.verdict.value.upper()}`",
                f"- **Код возврата Codex:** `{last_verification.exit_code}`",
                f"- **Тесты запускались:** {'Да' if last_verification.tests_executed else 'Нет'}",
                f"- **Тесты пройдены:** {'Да' if last_verification.tests_passed else 'Нет'}",
                "",
                "**Замечания верификатора:**",
                "```",
                last_verification.feedback or "Нет замечаний.",
                "```",
            ])
        else:
            report_lines.append("Верификация не проводилась (задача прервана до этапа проверки).")

        if task.error_message:
            report_lines.extend([
                "",
                "## 4. Ошибки и исключения",
                f"```\n{task.error_message}\n```",
            ])

        if task.git_diff:
            report_lines.extend([
                "",
                "## 5. Итоговый Git Diff (выборка)",
                "```diff",
                task.git_diff[:4000],
                "```",
            ])

        if events:
            report_lines.extend([
                "",
                "## 6. История жизненного цикла (Аудит-лог)",
                "| Время | Событие | Детали |",
                "| --- | --- | --- |",
            ])
            for ev in events:
                details = str(ev.payload)[:80].replace("|", "/")
                report_lines.append(f"| {ev.created_at} | `{ev.event_type}` | {details} |")

        return "\n".join(report_lines)
