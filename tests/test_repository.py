from __future__ import annotations

from pathlib import Path
import pytest
from agent_orchestrator.models import Task, TaskStatus
from agent_orchestrator.repository import ConcurrencyLimitError, RepositoryError, TaskRepository


def test_create_and_get_task(repo: TaskRepository):
    task = Task(title="Repo Task", prompt="Create repository")
    created = repo.create_task(task)
    assert created.id == task.id

    fetched = repo.get_task(task.id)
    assert fetched is not None
    assert fetched.id == task.id
    assert fetched.title == "Repo Task"
    assert fetched.status == TaskStatus.QUEUED


def test_update_task(repo: TaskRepository):
    task = Task(title="Update Task", prompt="Do work")
    repo.create_task(task)

    task.mark_running(bridge_task_id="br_100")
    task.antigravity_result = "Done partial"
    repo.update_task(task)

    fetched = repo.get_task(task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.RUNNING
    assert fetched.bridge_task_id == "br_100"
    assert fetched.antigravity_result == "Done partial"


@pytest.mark.parametrize("stale_status", [TaskStatus.RUNNING, TaskStatus.COMPLETED])
def test_update_task_cannot_overwrite_cancelled(repo: TaskRepository, stale_status):
    task = repo.create_task(Task(title="Отмена", prompt="Проверка"))
    stale = repo.get_task(task.id)
    task.mark_cancelled("Отменено пользователем.")
    repo.update_task(task)
    original_finished_at = task.finished_at
    if stale_status == TaskStatus.COMPLETED:
        stale.mark_completed()
    else:
        stale.mark_running()
    returned = repo.update_task(stale)
    stored = repo.get_task(task.id)
    assert stored.status == returned.status == TaskStatus.CANCELLED
    assert stored.error_message == returned.error_message == "Отменено пользователем."
    assert stored.finished_at == returned.finished_at == original_finished_at


def test_update_task_can_complete_running(repo: TaskRepository):
    task = repo.create_task(Task(title="Завершение", prompt="Проверка"))
    task.mark_running()
    repo.update_task(task)
    task.mark_completed()
    repo.update_task(task)
    assert task.status == repo.get_task(task.id).status == TaskStatus.COMPLETED


def test_concurrency_limit_enforced(repo: TaskRepository):
    task1 = Task(title="Task 1", prompt="Active 1")
    repo.create_task(task1, max_active=1)

    task2 = Task(title="Task 2", prompt="Active 2")
    with pytest.raises(ConcurrencyLimitError):
        repo.create_task(task2, max_active=1)

    # Complete task1 -> Now task2 can be created
    task1.mark_completed()
    repo.update_task(task1)

    task2_created = repo.create_task(task2, max_active=1)
    assert task2_created.id == task2.id


def test_get_by_bridge_id(repo: TaskRepository):
    task = Task(title="Bridge Search", prompt="Search")
    task.bridge_task_id = "br_unique_xyz"
    repo.create_task(task)

    found = repo.get_task_by_bridge_id("br_unique_xyz")
    assert found is not None
    assert found.id == task.id


def test_get_interrupted_tasks_recovery(repo: TaskRepository):
    t_queued = Task(title="Queued", prompt="P1", status=TaskStatus.QUEUED)
    t_running = Task(title="Running", prompt="P2", status=TaskStatus.RUNNING)
    t_completed = Task(title="Completed", prompt="P3", status=TaskStatus.COMPLETED)

    # Disable concurrency limit for this setup test
    repo.create_task(t_queued, max_active=0)
    repo.create_task(t_running, max_active=0)
    repo.create_task(t_completed, max_active=0)

    interrupted = repo.get_interrupted_tasks()
    interrupted_ids = [t.id for t in interrupted]
    assert t_queued.id in interrupted_ids
    assert t_running.id in interrupted_ids
    assert t_completed.id not in interrupted_ids


def test_audit_events_logging(repo: TaskRepository):
    task = Task(title="Audit", prompt="P")
    repo.create_task(task)

    repo.record_event(task.id, "TEST_EVENT_1", {"key": "value1"})
    repo.record_event(task.id, "TEST_EVENT_2", {"key": "value2"})

    events = repo.get_events(task.id)
    # create_task also logs TASK_CREATED
    assert len(events) == 3
    assert events[0].event_type == "TASK_CREATED"
    assert events[1].event_type == "TEST_EVENT_1"
    assert events[1].payload == {"key": "value1"}
    assert events[2].event_type == "TEST_EVENT_2"


def test_sqlite_connection_closed_allows_db_file_deletion(tmp_path: Path):
    db_file = tmp_path / "lifecycle_no_leak.sqlite"
    repo = TaskRepository(db_file)

    task = Task(title="Leak Check", prompt="Verify closed connections")
    repo.create_task(task)

    task.mark_running(bridge_task_id="br_leak_test")
    repo.update_task(task)

    fetched = repo.get_task(task.id)
    assert fetched is not None
    assert fetched.id == task.id

    by_bridge = repo.get_task_by_bridge_id("br_leak_test")
    assert by_bridge is not None

    active = repo.get_active_task()
    assert active is not None

    interrupted = repo.get_interrupted_tasks()
    assert len(interrupted) >= 1

    listed = repo.list_tasks()
    assert len(listed) >= 1

    repo.record_event(task.id, "TEST_EVENT", {"detail": "verify"})
    events = repo.get_events(task.id)
    assert len(events) >= 1

    # On Windows, open connections lock the file with WinError 32.
    # Without calling gc.collect(), deleting the file must succeed.
    db_file.unlink()
    assert not db_file.exists()


def test_concurrency_limit_does_not_leave_db_locked(tmp_path: Path):
    db_file = tmp_path / "concurrency_lock.sqlite"
    repo = TaskRepository(db_file)

    task1 = Task(title="Task 1", prompt="Prompt 1")
    repo.create_task(task1, max_active=1)

    task2 = Task(title="Task 2", prompt="Prompt 2")
    with pytest.raises(ConcurrencyLimitError):
        repo.create_task(task2, max_active=1)

    # Database must not be locked; subsequent operations should work
    fetched1 = repo.get_task(task1.id)
    assert fetched1 is not None and fetched1.id == task1.id

    task1.mark_completed()
    repo.update_task(task1)

    task2_created = repo.create_task(task2, max_active=1)
    assert task2_created.id == task2.id

    fetched2 = repo.get_task(task2.id)
    assert fetched2 is not None and fetched2.id == task2.id
