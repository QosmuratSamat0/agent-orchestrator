from __future__ import annotations

from agent_orchestrator.models import TaskStatus


COMPLETED_STATUSES = {
    TaskStatus.COMPLETED.value,
    "finished",
    "done",
    "success",
    "succeeded",
}

FAILED_STATUSES = {
    TaskStatus.FAILED.value,
    "error",
    "failed",
}

CANCELLED_STATUSES = {
    TaskStatus.CANCELLED.value,
    "cancelled",
    "canceled",
    "killed",
    "aborted",
}

RUNNING_STATUSES = {
    TaskStatus.RUNNING.value,
    "in_progress",
    "executing",
    "working",
    "processing",
}

QUEUED_STATUSES = {
    TaskStatus.QUEUED.value,
    "pending",
    "scheduled",
    "waiting",
}


def classify_bridge_status(raw: object) -> str:
    if not isinstance(raw, str):
        return "unknown"
    status = raw.lower().strip()
    for category, aliases in (
        ("completed", COMPLETED_STATUSES),
        ("failed", FAILED_STATUSES),
        ("cancelled", CANCELLED_STATUSES),
        ("running", RUNNING_STATUSES),
        ("queued", QUEUED_STATUSES),
    ):
        if status in aliases:
            return category
    return "unknown"
