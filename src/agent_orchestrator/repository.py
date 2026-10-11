from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional

from agent_orchestrator.models import (
    Task,
    TaskComplexity,
    TaskEvent,
    TaskStatus,
    VerificationVerdict,
)


class RepositoryError(Exception):
    pass


class ConcurrencyLimitError(RepositoryError):
    pass


class TaskRepository:
    def __init__(self, db_path: str | Path = "orchestrator.sqlite"):
        self.db_path = str(db_path)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        with closing(conn):
            with conn:
                yield conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    acceptance_criteria TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    complexity TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    model_reason TEXT,
                    bridge_task_id TEXT,
                    bridge_session_id TEXT,
                    request_id TEXT NOT NULL UNIQUE,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    current_attempt INTEGER NOT NULL DEFAULT 1,
                    antigravity_result TEXT,
                    git_diff TEXT,
                    verification_verdict TEXT,
                    verification_feedback TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_bridge_task_id ON tasks(bridge_task_id);

                CREATE TABLE IF NOT EXISTS task_events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
                """
            )
            conn.commit()

    def create_task(self, task: Task, max_active: int = 1) -> Task:
        with self._get_connection() as conn:
            if max_active > 0:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN (?, ?)",
                    (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
                )
                active_count = cursor.fetchone()[0]
                if active_count >= max_active:
                    raise ConcurrencyLimitError(
                        f"Active task limit reached ({active_count}/{max_active}). "
                        "Cannot start a new task until current task completes."
                    )

            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, prompt, acceptance_criteria, cwd, status, complexity,
                    model, effort, model_reason, bridge_task_id, bridge_session_id,
                    request_id, retry_count, max_retries, current_attempt,
                    antigravity_result, git_diff, verification_verdict,
                    verification_feedback, error_message, created_at, started_at,
                    finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.title,
                    task.prompt,
                    task.acceptance_criteria,
                    task.cwd,
                    task.status.value,
                    task.complexity.value,
                    task.model,
                    task.effort,
                    task.model_reason,
                    task.bridge_task_id,
                    task.bridge_session_id,
                    task.request_id,
                    task.retry_count,
                    task.max_retries,
                    task.current_attempt,
                    task.antigravity_result,
                    task.git_diff,
                    (
                        task.verification_verdict.value
                        if task.verification_verdict
                        else None
                    ),
                    task.verification_feedback,
                    task.error_message,
                    task.created_at,
                    task.started_at,
                    task.finished_at,
                    task.updated_at,
                ),
            )
            conn.commit()
        self.record_event(
            task.id,
            "TASK_CREATED",
            {"status": task.status.value, "model": task.model},
        )
        return task

    def update_task(self, task: Task) -> Task:
        task.updated_at = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks SET
                    title = ?, prompt = ?, acceptance_criteria = ?, cwd = ?,
                    status = ?, complexity = ?, model = ?, effort = ?, model_reason = ?,
                    bridge_task_id = ?, bridge_session_id = ?, retry_count = ?,
                    max_retries = ?, current_attempt = ?, antigravity_result = ?,
                    git_diff = ?, verification_verdict = ?, verification_feedback = ?,
                    error_message = ?, started_at = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    task.title,
                    task.prompt,
                    task.acceptance_criteria,
                    task.cwd,
                    task.status.value,
                    task.complexity.value,
                    task.model,
                    task.effort,
                    task.model_reason,
                    task.bridge_task_id,
                    task.bridge_session_id,
                    task.retry_count,
                    task.max_retries,
                    task.current_attempt,
                    task.antigravity_result,
                    task.git_diff,
                    (
                        task.verification_verdict.value
                        if task.verification_verdict
                        else None
                    ),
                    task.verification_feedback,
                    task.error_message,
                    task.started_at,
                    task.finished_at,
                    task.updated_at,
                    task.id,
                ),
            )
            if cursor.rowcount == 0:
                raise RepositoryError(f"Task with ID {task.id} not found.")
            conn.commit()
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def get_task_by_bridge_id(self, bridge_task_id: str) -> Optional[Task]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tasks WHERE bridge_task_id = ?", (bridge_task_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def get_active_task(self) -> Optional[Task]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks 
                WHERE status IN (?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def list_tasks(
        self, status: Optional[TaskStatus] = None, limit: int = 50
    ) -> List[Task]:
        with self._get_connection() as conn:
            if status:
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def get_interrupted_tasks(self) -> List[Task]:
        """Returns tasks that were running or queued when process stopped."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                ORDER BY created_at ASC
                """,
                (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value),
            )
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def record_event(
        self, task_id: str, event_type: str, payload: Dict[str, Any]
    ) -> TaskEvent:
        event = TaskEvent(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO task_events (id, task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.task_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at,
                ),
            )
            conn.commit()
        return event

    def get_events(self, task_id: str) -> List[TaskEvent]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            )
            events = []
            for row in cursor.fetchall():
                payload = {}
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    pass
                events.append(
                    TaskEvent(
                        id=row["id"],
                        task_id=row["task_id"],
                        event_type=row["event_type"],
                        payload=payload,
                        created_at=row["created_at"],
                    )
                )
            return events

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        data = dict(row)
        return Task.from_dict(data)
