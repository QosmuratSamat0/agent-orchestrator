from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from agent_orchestrator.bridge_client import BridgeClient, BridgeError
from agent_orchestrator.statuses import (
    CANCELLED_STATUSES,
    COMPLETED_STATUSES,
    FAILED_STATUSES,
    QUEUED_STATUSES,
    RUNNING_STATUSES,
)

logger = logging.getLogger("agent_orchestrator.waiter")


class TaskTimeoutError(Exception):
    pass


class TaskCancelledError(Exception):
    pass


class NonLLMWaiter:
    """Waits for task completion via Bridge without LLM polling overhead."""

    def __init__(
        self,
        bridge: BridgeClient,
        wait_chunk_sec: float = 60.0,
        total_timeout_sec: float = 2700.0,
        max_consecutive_errors: int = 5,
        base_backoff_sec: float = 1.0,
        max_backoff_sec: float = 16.0,
    ):
        self.bridge = bridge
        self.wait_chunk_sec = wait_chunk_sec
        self.total_timeout_sec = total_timeout_sec
        self.max_consecutive_errors = max_consecutive_errors
        self.base_backoff_sec = base_backoff_sec
        self.max_backoff_sec = max_backoff_sec

    def _sleep_with_checks(
        self,
        duration: float,
        start_time: float,
        bridge_task_id: str,
        should_cancel: Optional[Callable[[], bool]],
        total_timeout: Optional[float] = None,
    ) -> None:
        """Sleeps in small increments while checking for user cancellation and total timeout."""
        effective_timeout = (
            total_timeout if total_timeout is not None else self.total_timeout_sec
        )
        step = 0.2
        deadline = time.time() + duration
        while time.time() < deadline:
            if should_cancel and should_cancel():
                logger.warning(
                    f"Cancellation requested for task {bridge_task_id} during sleep."
                )
                try:
                    self.bridge.cancel_task(bridge_task_id)
                except Exception as e:
                    logger.warning(f"Error cancelling task in bridge: {e}")
                raise TaskCancelledError(f"Task {bridge_task_id} was cancelled.")

            elapsed = time.time() - start_time
            if elapsed >= effective_timeout:
                logger.error(
                    f"Task {bridge_task_id} exceeded total timeout of {effective_timeout}s during sleep"
                )
                try:
                    self.bridge.cancel_task(bridge_task_id)
                except Exception as e:
                    logger.warning(
                        f"Error cancelling task in bridge after timeout: {e}"
                    )
                raise TaskTimeoutError(
                    f"Task {bridge_task_id} timed out after {elapsed:.1f}s"
                )

            sleep_time = min(
                step,
                deadline - time.time(),
                max(0.0, effective_timeout - elapsed),
            )
            if sleep_time > 0:
                time.sleep(sleep_time)

    def wait_for_completion(
        self,
        bridge_task_id: str,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        total_timeout_sec: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Blocks until task reaches a terminal status or total timeout elapses."""
        effective_timeout = (
            min(self.total_timeout_sec, total_timeout_sec)
            if total_timeout_sec is not None
            else self.total_timeout_sec
        )
        start_time = time.time()
        logger.info(
            f"Waiting for task {bridge_task_id} without LLM polling "
            f"(chunk={self.wait_chunk_sec}s, total_max={effective_timeout}s)"
        )
        consecutive_errors = 0

        while True:
            # Check user cancellation
            if should_cancel and should_cancel():
                logger.warning(f"Cancellation requested for task {bridge_task_id}")
                try:
                    self.bridge.cancel_task(bridge_task_id)
                except Exception as e:
                    logger.warning(f"Error cancelling task in bridge: {e}")
                raise TaskCancelledError(f"Task {bridge_task_id} was cancelled.")

            # Calculate remaining time
            elapsed = time.time() - start_time
            remaining = effective_timeout - elapsed
            if remaining <= 0:
                logger.error(
                    f"Task {bridge_task_id} exceeded total timeout of {effective_timeout}s"
                )
                try:
                    self.bridge.cancel_task(bridge_task_id)
                except Exception as e:
                    logger.warning(
                        f"Error cancelling task in bridge after timeout: {e}"
                    )
                raise TaskTimeoutError(
                    f"Task {bridge_task_id} timed out after {elapsed:.1f}s"
                )

            current_chunk = min(self.wait_chunk_sec, remaining)
            resp: Optional[Dict[str, Any]] = None
            poll_error: Optional[str] = None

            # Attempt 1: wait_task
            try:
                resp = self.bridge.wait_task(bridge_task_id, timeout_sec=current_chunk)
                if not isinstance(resp, dict):
                    poll_error = (
                        f"Invalid response type from wait_task: {type(resp).__name__}"
                    )
                    resp = None
                elif not resp.get("ok", True):
                    err_msg = resp.get("error", "ok=false")
                    logger.warning(f"wait_task returned ok=false: {err_msg}")
                    poll_error = f"wait_task ok=false: {err_msg}"
                    resp = None
            except Exception as e:
                logger.warning(f"wait_task RPC error: {e}")
                poll_error = f"wait_task exception: {e}"
                resp = None

            # If wait_task failed or returned ok=false, attempt fallback check_task
            if resp is None:
                try:
                    check_resp = self.bridge.check_task(bridge_task_id)
                    if isinstance(check_resp, dict) and check_resp.get("ok", True):
                        resp = check_resp
                        poll_error = None
                    else:
                        err_msg = (
                            check_resp.get("error", "check_task ok=false")
                            if isinstance(check_resp, dict)
                            else str(check_resp)
                        )
                        logger.warning(f"check_task fallback also failed: {err_msg}")
                        poll_error = f"{poll_error}; check_task failed: {err_msg}"
                except Exception as e:
                    logger.warning(f"check_task fallback exception: {e}")
                    poll_error = f"{poll_error}; check_task exception: {e}"

            # If both wait_task and check_task failed
            if resp is None or poll_error is not None:
                consecutive_errors += 1
                logger.warning(
                    f"Task {bridge_task_id} polling error ({consecutive_errors}/{self.max_consecutive_errors}): {poll_error}"
                )
                if consecutive_errors >= self.max_consecutive_errors:
                    raise BridgeError(
                        f"Task {bridge_task_id} polling failed permanently after {consecutive_errors} consecutive errors: {poll_error}"
                    )
                backoff = min(
                    self.max_backoff_sec,
                    self.base_backoff_sec * (2 ** (consecutive_errors - 1)),
                )
                self._sleep_with_checks(
                    backoff,
                    start_time,
                    bridge_task_id,
                    should_cancel,
                    total_timeout=effective_timeout,
                )
                continue

            # A valid response with ok=True was received
            raw_status = resp.get("status")
            status_str = (
                str(raw_status).lower().strip() if raw_status is not None else ""
            )

            if on_progress:
                try:
                    on_progress(resp)
                except Exception as e:
                    logger.warning(f"Error in on_progress callback: {e}")

            logger.debug(
                f"Task {bridge_task_id} status: '{status_str}', elapsed: {time.time() - start_time:.1f}s"
            )

            # Check status categories
            if status_str in COMPLETED_STATUSES:
                consecutive_errors = 0
                logger.info(f"Task {bridge_task_id} completed successfully.")
                return resp

            elif status_str in FAILED_STATUSES:
                consecutive_errors = 0
                logger.warning(f"Task {bridge_task_id} failed: {resp.get('error')}")
                return resp

            elif status_str in CANCELLED_STATUSES:
                consecutive_errors = 0
                logger.warning(f"Task {bridge_task_id} was cancelled by bridge.")
                return resp

            elif status_str in RUNNING_STATUSES:
                consecutive_errors = 0
                self._sleep_with_checks(0.2, start_time, bridge_task_id, should_cancel, total_timeout=effective_timeout)
                continue

            elif status_str in QUEUED_STATUSES:
                consecutive_errors = 0
                self._sleep_with_checks(0.2, start_time, bridge_task_id, should_cancel, total_timeout=effective_timeout)
                continue

            else:
                # Unknown status string (e.g. "", "unknown", "unexpected")
                consecutive_errors += 1
                logger.warning(
                    f"Task {bridge_task_id} returned unknown status '{raw_status}' "
                    f"({consecutive_errors}/{self.max_consecutive_errors})"
                )
                if consecutive_errors >= self.max_consecutive_errors:
                    raise BridgeError(
                        f"Task {bridge_task_id} polling failed: received unknown status '{raw_status}' "
                        f"{consecutive_errors} times consecutively."
                    )
                backoff = min(
                    self.max_backoff_sec,
                    self.base_backoff_sec * (2 ** (consecutive_errors - 1)),
                )
                self._sleep_with_checks(
                    backoff, start_time, bridge_task_id, should_cancel,
                    total_timeout=effective_timeout,
                )
                continue
