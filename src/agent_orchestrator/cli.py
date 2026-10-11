from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import Task, TaskStatus
from agent_orchestrator.model_router import VERIFIED_AGY_MODELS
from agent_orchestrator.orchestrator import Orchestrator

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=format_str, datefmt="%H:%M:%S")


def print_step(step: str, task: Task) -> None:
    if step == "waiting_antigravity":
        msg = "⏳ [Этап 2] Ожидание завершения Antigravity (без расхода токенов LLM)..."
    elif step.startswith("dispatching_antigravity_"):
        attempt_str = step.removeprefix("dispatching_antigravity_")
        if attempt_str == "1":
            msg = "🚀 [Этап 1] Отправка задания в Antigravity CLI..."
        else:
            msg = f"🔄 [Этап 1] Отправка доработок в Antigravity CLI (Попытка {attempt_str})..."
    elif step.startswith("verifying_codex_"):
        attempt_str = step.removeprefix("verifying_codex_")
        msg = f"🔍 [Этап 3] Автоматический запуск верификатора Codex (Попытка {attempt_str})..."
    else:
        msg = f"📌 [Этап] {step}"
    print(f"\n{msg}\n")


def cmd_run(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    print("=" * 60)
    print("🤖 AGENT ORCHESTRATOR - ЗАПУСК КООРДИНАЦИИ")
    print("=" * 60)
    print(f"Заголовок: {args.title}")
    print(f"Каталог:   {args.cwd or os.getcwd()}")

    task = orchestrator.create_task(
        title=args.title,
        prompt=args.prompt,
        acceptance_criteria=args.criteria or "",
        cwd=args.cwd,
        model=args.model,
        effort=args.effort,
    )

    print(f"ID задачи: {task.id}")
    print(f"Выбранная модель: {task.model} (effort: {task.effort})")
    print(f"Обоснование:      {task.model_reason}")

    completed_task = orchestrator.run_task(task.id, on_step_callback=print_step)

    print("\n" + "=" * 60)
    print(f"🏁 РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ: {completed_task.status.value.upper()}")
    print("=" * 60)
    if completed_task.status == TaskStatus.COMPLETED:
        print("✅ Задача успешно выполнена и полностью подтверждена верификатором Codex!")
    else:
        print(f"❌ Задача завершилась со статусом: {completed_task.status.value}")
        if completed_task.error_message:
            print(f"Причина: {completed_task.error_message}")

    report_path = Path(completed_task.cwd) / f"orchestrator_report_{completed_task.id[:8]}.md"
    if report_path.exists():
        print(f"\n📄 Итоговый отчёт сохранён: {report_path}")


def cmd_submit(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    task = orchestrator.create_task(
        title=args.title,
        prompt=args.prompt,
        acceptance_criteria=args.criteria or "",
        cwd=args.cwd,
        model=args.model,
        effort=args.effort,
    )
    print(f"Задача добавлена в очередь! ID: {task.id}")
    print(f"Модель: {task.model} (effort: {task.effort})")


def cmd_status(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    task = orchestrator.repo.get_task(args.task_id) if args.task_id else orchestrator.repo.get_active_task()
    if not task:
        print("Задача не найдена.")
        return

    print("=" * 50)
    print(f"Задача:    {task.title} ({task.id})")
    print(f"Статус:    {task.status.value}")
    print(f"Модель:    {task.model} ({task.effort})")
    print(f"Попыток:   {task.retry_count}")
    print(f"Создана:   {task.created_at}")
    print(f"Обновлена: {task.updated_at}")
    if task.verification_verdict:
        print(f"Вердикт:   {task.verification_verdict.value}")
    if task.error_message:
        print(f"Ошибка:    {task.error_message}")
    print("=" * 50)


def cmd_list(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    status_filter = TaskStatus(args.status) if args.status else None
    tasks = orchestrator.repo.list_tasks(status=status_filter)

    print(f"{'ID':<10} {'СТАТУС':<12} {'МОДЕЛЬ':<24} {'ПОПЫТКИ':<8} {'ЗАГОЛОВОК'}")
    print("-" * 75)
    for t in tasks:
        print(f"{t.id[:8]:<10} {t.status.value:<12} {t.model:<24} {str(t.retry_count):<8} {t.title[:30]}")


def cmd_recover(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    print("Запуск процедуры восстановления прерванных задач после перезапуска...")
    recovered = orchestrator.recover()
    if not recovered:
        print("Прерванных задач не обнаружено.")
    else:
        print(f"Восстановлено и обработано задач: {len(recovered)}")
        for t in recovered:
            print(f"- {t.id[:8]}: {t.status.value}")


def cmd_cancel(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    task = orchestrator.cancel_task(args.task_id)
    print(f"Задача {task.id[:8]} переведена в статус: {task.status.value}")


def cmd_models(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    print("=== ПОДТВЕРЖДЁННЫЕ МОДЕЛИ ANTIGRAVITY (AGY MODELS) ===")
    for m in sorted(orchestrator.router.available_models):
        print(f"  • {m}")

    print("\n=== КОНФИГУРАЦИЯ МАРШРУТИЗАЦИИ ПО СЛОЖНОСТИ ===")
    print(f"  trivial  -> {config.models.trivial}")
    print(f"  simple   -> {config.models.simple}")
    print(f"  moderate -> {config.models.moderate}")
    print(f"  complex  -> {config.models.complex}")
    print(f"  expert   -> {config.models.expert}")
    print(f"  fallback -> {config.models.fallback}")


def cmd_report(args: argparse.Namespace) -> None:
    config = OrchestratorConfig.load(args.config)
    orchestrator = Orchestrator(config=config)

    task = orchestrator.repo.get_task(args.task_id)
    if not task:
        print(f"Задача с ID {args.task_id} не найдена.")
        return

    events = orchestrator.repo.get_events(task.id)
    report = orchestrator.retry_controller.generate_final_report(task, events=events)
    print(report)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-orchestrator",
        description="Локальный Python-оркестратор Codex CLI и Antigravity CLI через Agent-Bridge",
    )
    parser.add_argument("-c", "--config", help="Путь к файлу конфигурации TOML")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный лог (DEBUG)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="Создать и запустить задачу")
    p_run.add_argument("-t", "--title", required=True, help="Название задачи")
    p_run.add_argument("-p", "--prompt", required=True, help="Текст задания")
    p_run.add_argument("-a", "--criteria", help="Критерии приёмки")
    p_run.add_argument("-d", "--cwd", help="Рабочий каталог (по умолчанию текущий)")
    p_run.add_argument("-m", "--model", help="Принудительный выбор модели")
    p_run.add_argument("-e", "--effort", help="Уровень рассуждений (low/medium/high)")

    # submit
    p_sub = subparsers.add_parser("submit", help="Поставить задачу в очередь без немедленного запуска")
    p_sub.add_argument("-t", "--title", required=True, help="Название задачи")
    p_sub.add_argument("-p", "--prompt", required=True, help="Текст задания")
    p_sub.add_argument("-a", "--criteria", help="Критерии приёмки")
    p_sub.add_argument("-d", "--cwd", help="Рабочий каталог")
    p_sub.add_argument("-m", "--model", help="Принудительный выбор модели")
    p_sub.add_argument("-e", "--effort", help="Уровень рассуждений")

    # status
    p_stat = subparsers.add_parser("status", help="Проверить статус задачи")
    p_stat.add_argument("task_id", nargs="?", help="ID задачи (по умолчанию последняя активная)")

    # list
    p_list = subparsers.add_parser("list", help="Список всех задач")
    p_list.add_argument("-s", "--status", help="Фильтр по статусу")

    # recover
    subparsers.add_parser("recover", help="Восстановить прерванные задачи после сбоя/перезапуска")

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="Отменить задачу")
    p_cancel.add_argument("task_id", help="ID задачи")

    # models
    subparsers.add_parser("models", help="Показать доступные модели и матрицу маршрутизации")

    # report
    p_rep = subparsers.add_parser("report", help="Вывести диагностический отчёт задачи")
    p_rep.add_argument("task_id", help="ID задачи")

    args = parser.parse_args()
    setup_logging(args.verbose)

    dispatch_table = {
        "run": cmd_run,
        "submit": cmd_submit,
        "status": cmd_status,
        "list": cmd_list,
        "recover": cmd_recover,
        "cancel": cmd_cancel,
        "models": cmd_models,
        "report": cmd_report,
    }

    cmd_fn = dispatch_table.get(args.command)
    if cmd_fn:
        cmd_fn(args)


if __name__ == "__main__":
    main()
