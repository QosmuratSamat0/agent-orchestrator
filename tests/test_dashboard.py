from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import threading
from pathlib import Path

import pytest

from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.dashboard import DashboardApp, create_server
from agent_orchestrator.models import Task, TaskStatus
from agent_orchestrator.repository import TaskRepository


@pytest.fixture
def setup(tmp_path: Path):
    cfg = OrchestratorConfig(db_path=str(tmp_path / "tasks.sqlite"))
    cfg.progress.poll_interval_sec = 10.0
    repo = TaskRepository(cfg.db_path)
    running = Task(title="Живая <b>задача</b>", prompt="Исправь calc", cwd=str(tmp_path))
    repo.create_task(running, max_active=0)
    running.mark_running(bridge_task_id="br_1")
    repo.update_task(running)
    repo.record_event(running.id, "TASK_DISPATCHED", {"attempt": 1})
    done = Task(title="Готово", prompt="p", cwd=str(tmp_path))
    repo.create_task(done, max_active=0)
    done.mark_completed()
    repo.update_task(done)
    log_dir = tmp_path / "orchestrator_logs"
    log_dir.mkdir()
    (log_dir / f"{running.id[:8]}.log").write_text("строка 1\nстрока 2\n", encoding="utf-8")
    return cfg, repo, running, done, log_dir


@pytest.fixture
def server(setup):
    cfg = setup[0]
    app = DashboardApp(cfg, token="secret-token")
    srv = create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, app
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, headers=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    all_headers = {"Host": f"127.0.0.1:{port}"}
    all_headers.update(headers or {})
    conn.request(method, path, headers=all_headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, response.getheader("Content-Type", ""), body, response


def test_list_detail_and_remaining(setup):
    cfg, repo, running, done, _ = setup
    app = DashboardApp(cfg)
    listing = app.list_tasks()
    by_id = {t["id"]: t for t in listing["tasks"]}
    assert by_id[running.id]["status"] == "running" and by_id[running.id]["remaining_sec"] is not None
    assert by_id[done.id]["remaining_sec"] is None
    detail = app.task_detail(running.id)
    assert detail["prompt"] == "Исправь calc"
    assert [e["type"] for e in detail["events"]] == ["TASK_CREATED", "TASK_DISPATCHED"]
    assert app.task_detail("missing") is None


def test_log_tail_is_incremental_and_resets_on_truncation(setup):
    cfg, _, running, _, log_dir = setup
    app = DashboardApp(cfg)
    first = app.log_tail(running.id, 0)
    assert first["lines"] == ["строка 1", "строка 2"]
    log = log_dir / f"{running.id[:8]}.log"
    with log.open("a", encoding="utf-8") as stream:
        stream.write("строка 3\nнеполная")
    second = app.log_tail(running.id, first["offset"])
    assert second["lines"] == ["строка 3"]  # an unfinished line waits for its newline
    log.write_text("новый\n", encoding="utf-8")
    assert app.log_tail(running.id, second["offset"])["lines"] == ["новый"]
    assert app.log_tail(running.id, -5)["lines"] == ["новый"]


def test_http_page_and_api(server, setup):
    srv, _ = server
    status, ctype, body, response = request(srv, "GET", "/")
    page = body.decode("utf-8")
    assert status == 200 and ctype.startswith("text/html")
    assert "secret-token" in page and "__DASHBOARD_TOKEN__" not in page
    assert "default-src 'none'" in response.getheader("Content-Security-Policy")
    assert "http://" not in page and "https://" not in page  # no external resources
    assert all(label in page for label in ("Последний час", "Сегодня", "Раньше"))  # task list groups
    status, ctype, body, _ = request(srv, "GET", "/api/tasks")
    assert status == 200 and ctype.startswith("application/json")
    titles = [t["title"] for t in json.loads(body)["tasks"]]
    assert "Живая <b>задача</b>" in titles  # raw text; the page inserts it with textContent
    running = setup[2]
    status, _, body, _ = request(srv, "GET", f"/api/tasks/{running.id}/log?offset=0")
    assert json.loads(body)["lines"] == ["строка 1", "строка 2"]
    assert request(srv, "GET", "/api/tasks/nope")[0] == 404
    assert request(srv, "GET", "/api/tasks/../../etc/passwd")[0] == 404
    assert request(srv, "GET", "/other")[0] == 404


def test_foreign_host_header_is_rejected(server):
    srv, _ = server
    assert request(srv, "GET", "/api/tasks", {"Host": "evil.example:80"})[0] == 403
    assert request(srv, "GET", "/", {"Host": "evil.example"})[0] == 403
    port = srv.server_address[1]
    assert request(srv, "GET", "/api/tasks", {"Host": f"localhost:{port}"})[0] == 200


def test_cancel_requires_token_and_origin(server, setup):
    srv, _ = server
    _, repo, running, _, _ = setup
    path = f"/api/tasks/{running.id}/cancel"
    assert request(srv, "POST", path)[0] == 403
    assert request(srv, "POST", path, {"X-Dashboard-Token": "wrong"})[0] == 403
    assert request(srv, "POST", path, {"X-Dashboard-Token": "secret-token", "Origin": "https://evil.example"})[0] == 403
    assert repo.get_task(running.id).status == TaskStatus.RUNNING


def test_cancel_marks_db_writes_report_and_starts_nothing(server, setup, monkeypatch):
    srv, _ = server
    _, repo, running, done, tmp = setup[0], setup[1], setup[2], setup[3], Path(setup[2].cwd)

    def forbidden(*_, **__):
        raise AssertionError("dashboard must not start processes")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    port = srv.server_address[1]
    status, _, body, _ = request(
        srv, "POST", f"/api/tasks/{running.id}/cancel",
        {"X-Dashboard-Token": "secret-token", "Origin": f"http://127.0.0.1:{port}"},
    )
    result = json.loads(body)
    assert status == 200 and result["cancelled"] is True and "10.0" in result["message"]
    assert repo.get_task(running.id).status == TaskStatus.CANCELLED
    assert (tmp / f"orchestrator_report_{running.id[:8]}.md").is_file()
    status, _, body, _ = request(srv, "POST", f"/api/tasks/{done.id}/cancel", {"X-Dashboard-Token": "secret-token"})
    assert json.loads(body)["cancelled"] is False and "уже завершена" in json.loads(body)["message"]
    assert repo.get_task(done.id).status == TaskStatus.COMPLETED


def test_cli_dashboard_missing_db_exits_without_creating(tmp_path, monkeypatch, capsys):
    from agent_orchestrator import cli

    db = tmp_path / "nope" / "x.sqlite"
    cfg = OrchestratorConfig(db_path=str(db))
    monkeypatch.setattr(cli.OrchestratorConfig, "load", lambda *_, **__: cfg)
    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_dashboard(argparse.Namespace(config=None, host="127.0.0.1", port=0, open=False))
    assert exit_info.value.code == 1
    assert "База данных не найдена" in capsys.readouterr().out
    assert not db.exists()
