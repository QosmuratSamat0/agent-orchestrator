from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import time
from typing import Callable, List, Optional
import uuid

from agent_orchestrator.bridge_client import BridgeClient, BridgeError
from agent_orchestrator.codex_verifier import CodexVerifier
from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import (
    Task,
    TaskStatus,
    VerificationResult,
    VerificationVerdict,
)
from agent_orchestrator.model_router import ModelRouter, dispatch_effort
from agent_orchestrator.progress import ProgressFeed
from agent_orchestrator.repository import ConcurrencyLimitError, TaskRepository
from agent_orchestrator.retry_controller import RetryController
from agent_orchestrator.statuses import classify_bridge_status
from agent_orchestrator.waiter import (
    NonLLMWaiter,
    TaskCancelledError,
    TaskTimeoutError,
)

logger = logging.getLogger("agent_orchestrator.orchestrator")


def _parse_iso_to_epoch(iso_str: str) -> float:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


BRIDGE_ERROR_TEXT = {
    # Set by a new Agent-Bridge process for tasks left by a process that died (seen in a real hard-crash run).
    "bridge_restarted": (
        "Agent-Bridge был перезапущен после аварийного завершения: выполнение задачи прервано. "
        "Проверьте изменения в рабочем каталоге и запустите задачу заново."
    ),
}


def _bridge_error_text(error: Optional[str], default: str) -> str:
    """Readable Russian text for known Agent-Bridge error codes; unknown errors are kept as they are."""
    if not error:
        return default
    return BRIDGE_ERROR_TEXT.get(str(error).strip(), str(error))


def _attempt_request_id(request_id: str, retry_count: int) -> str:
    try:
        namespace = uuid.UUID(request_id)
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{request_id}:{retry_count}"))
    return str(uuid.uuid5(namespace, str(retry_count)))


class Orchestrator:
    """Core Task Manager orchestrating Codex CLI and Antigravity CLI via Agent-Bridge."""

    def __init__(
        self,
        config: Optional[OrchestratorConfig] = None,
        repository: Optional[TaskRepository] = None,
        bridge: Optional[BridgeClient] = None,
        verifier: Optional[CodexVerifier] = None,
        router: Optional[ModelRouter] = None,
        retry_controller: Optional[RetryController] = None,
        waiter: Optional[NonLLMWaiter] = None,
        clock: Callable[[], float] = time.time,
        progress: Optional[ProgressFeed] = None,
    ):
        self.config = config or OrchestratorConfig.load()
        self.clock = clock
        self.repo = repository or TaskRepository(self.config.db_path)
        self.bridge = bridge or BridgeClient(self.config.bridge.command)
        self.verifier = verifier or CodexVerifier(
            codex_bin=self.config.codex.command,
            timeout_sec=self.config.codex.exec_timeout_sec,
            sandbox_mode=self.config.codex.sandbox_mode,
            skip_git_repo_check=self.config.codex.skip_git_repo_check,
            ephemeral=self.config.codex.ephemeral,
        )
        self._router = router
        self.retry_controller = retry_controller or RetryController()
        self.progress = progress
        if self.progress is None and self.config.progress.enabled:
            log_dir = self.config.progress.log_dir or str(
                Path(self.repo.db_path).parent / "orchestrator_logs"
            )
            self.progress = ProgressFeed(
                log_dir=Path(log_dir),
                poll_interval_sec=self.config.progress.poll_interval_sec,
            )
        self.waiter = waiter or NonLLMWaiter(
            bridge=self.bridge,
            wait_chunk_sec=(
                min(self.config.bridge.wait_chunk_sec, self.config.progress.poll_interval_sec)
                if self.progress and self.progress.enabled
                else self.config.bridge.wait_chunk_sec
            ),
            total_timeout_sec=self.config.total_task_timeout_sec,
        )

    def close(self) -> None:
        try:
            self.bridge.close()
        except Exception:
            logger.warning("Не удалось закрыть соединение Agent-Bridge", exc_info=True)

    def _log_progress(self, task: Task, message: str) -> None:
        if self.progress:
            try:
                self.progress.log(task, message)
            except Exception:
                logger.debug("Не удалось записать прогресс задачи", exc_info=True)

    def _wait_progress(self, task: Task, resp: dict, deadline: float) -> None:
        if self.progress:
            try:
                self.progress.on_wait_progress(task, self.bridge, resp, deadline_epoch=deadline)
            except Exception:
                logger.debug("Не удалось обработать прогресс ожидания", exc_info=True)

    @property
    def router(self) -> ModelRouter:
        if self._router is None:
            self._router = ModelRouter(
                config=self.config.models,
                agy_bin=self.config.antigravity.command,
            )
        return self._router

    def _finish(
        self, task: Task, last_verification: Optional[VerificationResult] = None
    ) -> Task:
        self._log_progress(task, f"Итоговый статус: {task.status.value}")
        try:
            events = self.repo.get_events(task.id)
            report_text = self.retry_controller.generate_final_report(
                task, last_verification, events
            )
            report_path = Path(task.cwd) / f"orchestrator_report_{task.id[:8]}.md"
            report_path.write_text(report_text, encoding="utf-8")
            logger.info(f"Итоговый отчёт сохранён: {report_path}")
        except Exception as e:
            logger.warning(f"Не удалось записать итоговый отчёт задачи {task.id}: {e}")
        return task

    def _cancelled_task(self, task_id: str, bridge_task_id: Optional[str] = None) -> Optional[Task]:
        current = self.repo.get_task(task_id)
        if current and current.status == TaskStatus.CANCELLED:
            if bridge_task_id:
                try:
                    self.bridge.cancel_task(bridge_task_id)
                except Exception as e:
                    logger.warning(f"Не удалось отменить задачу в Bridge: {e}")
            return current
        return None

    def _retry_pending(self, task: Task) -> bool:
        dispatched = next(
            (event for event in reversed(self.repo.get_events(task.id))
             if event.event_type == "TASK_DISPATCHED"),
            None,
        )
        return bool(dispatched and task.retry_count + 1 > dispatched.payload.get("attempt", 0))

    def create_task(
        self,
        title: str,
        prompt: str,
        acceptance_criteria: str = "",
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Task:
        effective_cwd = cwd or os.getcwd()
        selection = self.router.select_model(
            prompt=prompt,
            acceptance_criteria=acceptance_criteria,
            manual_model=model,
            manual_effort=effort,
        )

        task = Task(
            title=title,
            prompt=prompt,
            acceptance_criteria=acceptance_criteria,
            cwd=str(Path(effective_cwd).resolve()),
            status=TaskStatus.QUEUED,
            complexity=selection.complexity,
            model=selection.model,
            effort=selection.effort,
            model_reason=selection.reason,
            max_retries=0,
        )

        created_task = self.repo.create_task(
            task, max_active=self.config.max_active_tasks
        )
        logger.info(
            f"Created task '{created_task.title}' (ID: {created_task.id}) with model {created_task.model}"
        )
        return created_task

    def run_task(
        self,
        task_id: str,
        on_step_callback: Optional[Callable[[str, Task], None]] = None,
        resume: Optional[str] = None,
        resume_running: bool = False,
    ) -> Task:
        task = self.repo.get_task(task_id)
        if not task:
            raise ValueError(f"Task with ID {task_id} not found.")

        if task.status.is_terminal:
            logger.info(f"Task {task.id} is already in terminal state: {task.status.value}")
            return self._finish(task)

        if isinstance(resume, bool):
            resume = "wait" if resume else None
        if resume_running and resume is None:
            resume = "wait"
        if resume not in (None, "wait", "verify"):
            raise ValueError(f"Недопустимый режим восстановления: {resume}")
        if resume and not task.bridge_task_id:
            raise ValueError("Для восстановления нужен идентификатор задачи Bridge.")

        if not task.started_at:
            task.mark_running()
            task.started_at = datetime.fromtimestamp(
                self.clock(), tz=timezone.utc
            ).isoformat()
            task.updated_at = task.started_at
            self.repo.update_task(task)

        start_epoch = _parse_iso_to_epoch(task.started_at)
        deadline = start_epoch + self.config.total_task_timeout_sec

        current_prompt = task.prompt
        if self._retry_pending(task):
            previous_verification = VerificationResult(
                verdict=VerificationVerdict.RETRY_NEEDED,
                feedback=task.verification_feedback or "",
                exit_code=1,
                raw_output="",
                issues_found=[],
            )
            current_prompt = self.retry_controller.build_refinement_prompt(
                task, previous_verification
            )
        last_verification: Optional[VerificationResult] = None
        is_first_iteration = True

        while True:
            cancelled = self._cancelled_task(task.id, task.bridge_task_id)
            if cancelled:
                return self._finish(cancelled, last_verification)
            remaining = deadline - self.clock()
            if remaining <= 0:
                phase = (
                    "before_resume"
                    if is_first_iteration and (resume or bool(task.bridge_task_id))
                    else "before_dispatch"
                )
                if task.bridge_task_id and (phase == "before_resume" or is_first_iteration):
                    try:
                        self.bridge.cancel_task(task.bridge_task_id)
                    except Exception as e:
                        logger.warning(
                            f"Could not cancel bridge task {task.bridge_task_id} on timeout: {e}"
                        )
                task.mark_timed_out(
                    f"Превышен общий лимит времени задачи ({int(self.config.total_task_timeout_sec)} сек.)."
                )
                self.repo.update_task(task)
                self.repo.record_event(
                    task.id,
                    "TASK_TIMED_OUT",
                    {
                        "phase": phase,
                        "budget_sec": self.config.total_task_timeout_sec,
                    },
                )
                break

            attempt_label = f"Attempt {task.retry_count + 1}"

            # Step 1: Dispatch to Antigravity (skip if resuming already running task)
            resume_mode = resume if is_first_iteration else None
            if resume_mode in ("wait", "verify"):
                bridge_task_id = task.bridge_task_id
                logger.info(
                    f"[{attempt_label}] Восстанавливаю {'ожидание' if resume_mode == 'wait' else 'проверку'} "
                    f"задачи {task.id} (Bridge ID: {bridge_task_id})..."
                )
            else:
                logger.info(f"[{attempt_label}] Dispatching to Antigravity via Agent-Bridge...")
                if on_step_callback:
                    on_step_callback(f"dispatching_antigravity_{task.retry_count + 1}", task)

                cancelled = self._cancelled_task(task.id, task.bridge_task_id)
                if cancelled:
                    return self._finish(cancelled, last_verification)

                try:
                    self._log_progress(
                        task, f"Отправка попытки {task.retry_count + 1}, модель {task.model}"
                    )
                    dispatch_resp = self.bridge.dispatch_task(
                        agent="antigravity",
                        message=current_prompt,
                        cwd=task.cwd,
                        model=task.model,
                        effort=dispatch_effort(task.model, task.effort),
                        title=f"{task.title} (Attempt {task.retry_count + 1})",
                        request_id=_attempt_request_id(task.request_id, task.retry_count),
                        session_id=task.bridge_session_id,
                    )
                except Exception as e:
                    cancelled = self._cancelled_task(task.id)
                    if cancelled:
                        return self._finish(cancelled, last_verification)
                    err_msg = f"Failed to dispatch task to Agent-Bridge: {e}"
                    logger.error(err_msg)
                    task.mark_failed(err_msg)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "DISPATCH_FAILED", {"error": str(e)})
                    return self._finish(task, last_verification)

                bridge_task_id = dispatch_resp.get("task_id")
                cancelled = self._cancelled_task(task.id, bridge_task_id)
                if cancelled:
                    return self._finish(cancelled, last_verification)
                if dispatch_resp.get("ok") is False or not bridge_task_id:
                    error = str(dispatch_resp.get("error") or "Agent-Bridge не вернул task_id.")
                    error_type = dispatch_resp.get("error_type")
                    detail = (
                        f"{error_type}: {error}"
                        if error_type and not error.startswith(f"{error_type}:")
                        else error
                    )
                    err_msg = f"Не удалось отправить задачу в Agent-Bridge: {detail}"
                    task.mark_failed(err_msg)
                    self.repo.update_task(task)
                    self.repo.record_event(
                        task.id, "DISPATCH_FAILED", {"error": error, "error_type": error_type}
                    )
                    return self._finish(task, last_verification)

                session_id = dispatch_resp.get("session_id")

                task.bridge_task_id = bridge_task_id
                if session_id:
                    task.bridge_session_id = session_id
                task.mark_running(bridge_task_id=bridge_task_id)
                self.repo.update_task(task)
                self.repo.record_event(
                    task.id,
                    "TASK_DISPATCHED",
                    {
                        "bridge_task_id": bridge_task_id,
                        "attempt": task.retry_count + 1,
                        "model": task.model,
                    },
                )

            is_first_iteration = False

            if resume_mode != "verify":
                # Step 2: Wait for Antigravity completion without LLM
                if on_step_callback:
                    on_step_callback("waiting_antigravity", task)

                remaining_for_wait = deadline - self.clock()
                try:
                    wait_result = self.waiter.wait_for_completion(
                        bridge_task_id,
                        total_timeout_sec=remaining_for_wait,
                        should_cancel=lambda: self._cancelled_task(task.id) is not None,
                        on_progress=(
                            (lambda resp: self._wait_progress(task, resp, deadline))
                            if self.progress
                            else None
                        ),
                    )
                except TaskTimeoutError as e:
                    cancelled = self._cancelled_task(task.id, bridge_task_id)
                    if cancelled:
                        return self._finish(cancelled, last_verification)
                    task.mark_timed_out(str(e))
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_TIMED_OUT", {"error": str(e)})
                    break
                except TaskCancelledError as e:
                    cancelled = self._cancelled_task(task.id, bridge_task_id)
                    if cancelled:
                        return self._finish(cancelled, last_verification)
                    task.mark_cancelled(str(e))
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_CANCELLED", {"error": str(e)})
                    return self._finish(task, last_verification)
                except BridgeError as e:
                    cancelled = self._cancelled_task(task.id, bridge_task_id)
                    if cancelled:
                        return self._finish(cancelled, last_verification)
                    task.mark_failed(f"Сбой Agent-Bridge: {e}")
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"error": str(e)})
                    return self._finish(task, last_verification)

                # Check terminal state returned by waiter
                cancelled = self._cancelled_task(task.id, bridge_task_id)
                if cancelled:
                    return self._finish(cancelled, last_verification)
                bridge_status = classify_bridge_status(wait_result.get("status"))

                if bridge_status == "cancelled":
                    err_text = (
                        wait_result.get("error")
                        or wait_result.get("reason")
                        or "Задача была отменена в Agent-Bridge."
                    )
                    task.mark_cancelled(err_text)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_CANCELLED", {"error": err_text})
                    return self._finish(task, last_verification)

                if bridge_status == "failed":
                    err_text = _bridge_error_text(wait_result.get("error"), "Antigravity reported failure.")
                    task.mark_failed(err_text)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"error": err_text})
                    return self._finish(task, last_verification)

                if bridge_status != "completed":
                    err_text = f"Неожиданный статус завершения от Agent-Bridge: '{wait_result.get('status')}'"
                    task.mark_failed(err_text)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"error": err_text})
                    return self._finish(task, last_verification)

            # Step 3: Collect Antigravity results & Git diff
            logger.info("Collecting Antigravity results from Agent-Bridge...")
            try:
                res_payload = self.bridge.get_result(bridge_task_id)
                if not res_payload.get("ok", True):
                    cancelled = self._cancelled_task(task.id, bridge_task_id)
                    if cancelled:
                        return self._finish(cancelled, last_verification)
                    err_text = (
                        "Не удалось получить результат задачи из Agent-Bridge: "
                        f"{res_payload.get('error') or 'Bridge вернул ok=false.'}"
                    )
                    task.mark_failed(err_text)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"error": err_text})
                    return self._finish(task, last_verification)
                task.antigravity_result = (
                    res_payload.get("result_text") or str(res_payload)
                )
            except Exception as e:
                logger.warning(f"Could not retrieve full result: {e}")
                task.antigravity_result = str(wait_result)

            task.git_diff = self.verifier.capture_git_diff(task.cwd)
            cancelled = self._cancelled_task(task.id, bridge_task_id)
            if cancelled:
                return self._finish(cancelled, last_verification)
            self.repo.update_task(task)
            self.repo.record_event(
                task.id,
                "ANTIGRAVITY_FINISHED",
                {
                    "bridge_task_id": bridge_task_id,
                    "status": (
                        "completed" if resume_mode == "verify" else wait_result.get("status")
                    ),
                },
            )
            self._log_progress(task, "Работа Antigravity завершена")

            # Step 4: Run Codex Verifier
            logger.info(f"[{attempt_label}] Launching Codex Verifier process...")
            if on_step_callback:
                on_step_callback(f"verifying_codex_{task.retry_count + 1}", task)

            remaining_for_verify = max(1.0, deadline - self.clock())
            self._log_progress(task, "Проверка начата")
            verification = self.verifier.verify(
                original_prompt=task.prompt,
                acceptance_criteria=task.acceptance_criteria,
                antigravity_result=task.antigravity_result or "",
                cwd=task.cwd,
                attempt=task.retry_count + 1,
                timeout_sec=remaining_for_verify,
            )
            cancelled = self._cancelled_task(task.id, bridge_task_id)
            if cancelled:
                return self._finish(cancelled, last_verification)
            last_verification = verification
            task.verification_verdict = verification.verdict
            task.verification_feedback = verification.feedback
            self.repo.update_task(task)
            self.repo.record_event(
                task.id,
                "VERIFICATION_COMPLETED",
                {
                    "attempt": task.retry_count + 1,
                    "verdict": verification.verdict.value,
                    "exit_code": verification.exit_code,
                    "issues_count": len(verification.issues_found),
                },
            )
            self._log_progress(
                task,
                f"Вердикт: {verification.verdict.value}; {verification.feedback[:150]}",
            )

            # Step 5: Decision based on verdict
            if verification.verdict == VerificationVerdict.PASSED:
                logger.info("Codex Verifier PASSED! Task completed successfully.")
                task.mark_completed()
                self.repo.update_task(task)
                break

            remaining_after_verify = deadline - self.clock()
            if remaining_after_verify <= 0:
                logger.warning(
                    f"Task {task.id} deadline expired after verification ({int(self.config.total_task_timeout_sec)}s). Marking TIMED_OUT."
                )
                task.mark_timed_out(
                    f"Превышен общий лимит времени задачи ({int(self.config.total_task_timeout_sec)} сек.)."
                )
                self.repo.update_task(task)
                self.repo.record_event(
                    task.id,
                    "TASK_TIMED_OUT",
                    {
                        "phase": "after_verification",
                        "budget_sec": self.config.total_task_timeout_sec,
                    },
                )
                break

            if verification.verdict == VerificationVerdict.RETRY_NEEDED:
                task.retry_count += 1
                task.current_attempt += 1
                current_prompt = self.retry_controller.build_refinement_prompt(
                    task, verification
                )
                logger.info(
                    f"Retry needed. Starting attempt {task.retry_count + 1}."
                )
                self.repo.update_task(task)
                self.repo.record_event(
                    task.id,
                    "RETRY_INITIATED",
                    {"next_attempt": task.retry_count + 1},
                )
                self._log_progress(task, f"Повторная попытка {task.retry_count + 1} начата")
                continue
            else:  # FAILED
                logger.error(f"Codex Verifier rejected changes as FAILED: {verification.feedback}")
                task.mark_failed(f"Верификатор отклонил выполнение: {verification.feedback}")
                self.repo.update_task(task)
                break

        return self._finish(task, last_verification)

    def recover(self) -> List[Task]:
        """Detects interrupted tasks after orchestrator crash/restart and restores lifecycle."""
        interrupted = self.repo.get_interrupted_tasks()
        recovered: List[Task] = []

        if not interrupted:
            logger.info("No interrupted tasks found for recovery.")
            return recovered

        logger.info(f"Found {len(interrupted)} interrupted task(s). Starting recovery...")

        for task in interrupted:
            logger.info(f"Recovering task {task.id} (Bridge ID: {task.bridge_task_id})...")
            cancelled = self._cancelled_task(task.id)
            if cancelled:
                recovered.append(self._finish(cancelled))
                continue
            if not task.bridge_task_id:
                # Task was queued but not yet dispatched
                logger.info(f"Task {task.id} was queued. Starting execution now.")
                res_task = self.run_task(task.id)
                recovered.append(res_task)
                continue

            if self._retry_pending(task):
                recovered.append(self.run_task(task.id))
                continue

            try:
                # Query Agent-Bridge for current status of the task
                status_resp = self.bridge.check_task(task.bridge_task_id)
                cancelled = self._cancelled_task(task.id)
                if cancelled:
                    recovered.append(self._finish(cancelled))
                    continue
                if not status_resp.get("ok", True):
                    # Bridge doesn't know about this task (process was terminated)
                    logger.warning(
                        f"Task {task.id} not found in Bridge memory. Marking FAILED."
                    )
                    task.mark_failed(
                        "Процесс был прерван, задача не найдена в памяти Agent-Bridge."
                    )
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"error": task.error_message})
                    recovered.append(self._finish(task))
                    continue

                raw_status = status_resp.get("status")
                bridge_status = classify_bridge_status(raw_status)
                if bridge_status == "completed":
                    logger.info(
                        f"Task {task.id} completed while orchestrator was offline. Continuing with verification."
                    )
                    recovered.append(self.run_task(task.id, resume="verify"))
                elif bridge_status in ("running", "queued"):
                    if status_resp.get("from_state"):
                        task.mark_failed(
                            "Задача принадлежит другому или предыдущему экземпляру Agent-Bridge; "
                            "ожидание и отмена невозможны. Проверьте работу агента вручную "
                            "перед повторным запуском."
                        )
                        self.repo.update_task(task)
                        self.repo.record_event(
                            task.id,
                            "TASK_ORPHANED",
                            {"bridge_task_id": task.bridge_task_id, "status": raw_status},
                        )
                        recovered.append(self._finish(task))
                        continue
                    logger.info(
                        f"Task {task.id} is still running in Bridge ({bridge_status}). Resuming wait..."
                    )
                    res_task = self.run_task(task.id, resume="wait")
                    recovered.append(res_task)
                elif bridge_status == "cancelled":
                    reason = (
                        status_resp.get("reason")
                        or status_resp.get("error")
                        or "Задача была отменена в Bridge"
                    )
                    logger.warning(f"Task {task.id} was cancelled in Bridge: {reason}")
                    task.mark_cancelled(reason)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_CANCELLED", {"reason": reason})
                    recovered.append(self._finish(task))
                elif bridge_status == "failed":
                    error_msg = _bridge_error_text(
                        status_resp.get("error"), "Задача завершилась с ошибкой в Bridge"
                    )
                    logger.warning(f"Task {task.id} failed in Bridge: {error_msg}")
                    task.mark_failed(error_msg)
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"error": error_msg})
                    recovered.append(self._finish(task))
                else:
                    task.mark_failed(f"Статус задачи в Bridge: {raw_status}")
                    self.repo.update_task(task)
                    self.repo.record_event(task.id, "TASK_FAILED", {"status": raw_status})
                    recovered.append(self._finish(task))

            except Exception as e:
                logger.error(f"Failed to recover task {task.id}: {e}")
                cancelled = self._cancelled_task(task.id)
                if cancelled:
                    recovered.append(self._finish(cancelled))
                    continue
                task.mark_failed(f"Сбой восстановления: {e}")
                self.repo.update_task(task)
                self.repo.record_event(task.id, "TASK_FAILED", {"error": str(e)})
                recovered.append(self._finish(task))

        return recovered

    def cancel_task(self, task_id: str, *, notify_bridge: bool = True) -> Task:
        task = self.repo.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found.")

        if task.status.is_terminal:
            return task

        if notify_bridge and task.bridge_task_id:
            try:
                self.bridge.cancel_task(task.bridge_task_id)
            except Exception as e:
                logger.warning(f"Bridge cancel error: {e}")

        # Между повторным чтением и записью остаётся короткое окно гонки без CAS в БД.
        task = self.repo.get_task(task_id)
        if task.status.is_terminal:
            return task
        task.mark_cancelled("Отменено пользователем.")
        self.repo.update_task(task)
        self.repo.record_event(task.id, "USER_CANCELLED", {})
        return self._finish(task)
