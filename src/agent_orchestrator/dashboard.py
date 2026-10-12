from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.dashboard_page import PAGE_HTML
from agent_orchestrator.models import Task
from agent_orchestrator.repository import TaskRepository

logger = logging.getLogger("agent_orchestrator.dashboard")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_LOG_CHUNK = 256 * 1024
_TASK_PATH = re.compile(r"^/api/tasks/([0-9A-Za-z-]{1,64})(/log|/cancel)?$")


def log_dir_for(config: OrchestratorConfig) -> Path:
    """Same location rule as ProgressFeed and `watch`: [progress].log_dir or orchestrator_logs next to the DB."""
    if config.progress.log_dir:
        return Path(config.progress.log_dir)
    return Path(config.db_path).parent / "orchestrator_logs"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class DashboardApp:
    """Read-mostly view over the orchestrator's SQLite DB and live logs. Never starts Agent-Bridge or agy."""

    def __init__(
        self,
        config: OrchestratorConfig,
        token: Optional[str] = None,
        cancel_fn: Optional[Callable[[str], Task]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.repo = TaskRepository(config.db_path)
        self.token = token or secrets.token_urlsafe(24)
        self.cancel_fn = cancel_fn or self._cancel_in_db
        self.clock = clock

    def _cancel_in_db(self, task_id: str) -> Task:
        # Same path as `cli cancel`: the DB is the source of truth; the running orchestrator stops the worker.
        from agent_orchestrator.orchestrator import Orchestrator

        orchestrator = Orchestrator(config=self.config, repository=self.repo)
        try:
            return orchestrator.cancel_task(task_id, notify_bridge=False)
        finally:
            orchestrator.close()

    def _remaining_sec(self, task: Task) -> Optional[int]:
        started = _parse_iso(task.started_at)
        if task.status.is_terminal or started is None:
            return None
        return max(0, int(started.timestamp() + self.config.total_task_timeout_sec - self.clock()))

    def summary(self, task: Task) -> Dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "status": task.status.value,
            "is_terminal": task.status.is_terminal,
            "attempt": task.retry_count + 1,
            "model": task.model,
            "verdict": task.verification_verdict.value if task.verification_verdict else None,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "remaining_sec": self._remaining_sec(task),
        }

    def list_tasks(self, limit: int = 50) -> Dict[str, Any]:
        return {
            "tasks": [self.summary(task) for task in self.repo.list_tasks(limit=limit)],
            "budget_sec": self.config.total_task_timeout_sec,
        }

    def task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self.repo.get_task(task_id)
        if not task:
            return None
        data = self.summary(task)
        data.update(
            {
                "effort": task.effort,
                "cwd": task.cwd,
                "prompt": task.prompt,
                "acceptance_criteria": task.acceptance_criteria,
                "bridge_task_id": task.bridge_task_id,
                "feedback": (task.verification_feedback or "")[:4000],
                "error_message": task.error_message,
                "budget_sec": self.config.total_task_timeout_sec,
                "events": [
                    {"type": event.event_type, "at": event.created_at, "payload": event.payload}
                    for event in self.repo.get_events(task.id)[-200:]
                ],
            }
        )
        return data

    def log_tail(self, task_id: str, offset: int = 0) -> Optional[Dict[str, Any]]:
        """New complete lines of the live log since byte `offset`."""
        task = self.repo.get_task(task_id)
        if not task:
            return None
        path = log_dir_for(self.config) / f"{task.id[:8]}.log"
        if not path.is_file():
            return {"lines": [], "offset": 0}
        size = path.stat().st_size
        if offset < 0 or offset > size:
            offset = 0  # the log was truncated or the client is confused: start over
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(MAX_LOG_CHUNK)
        complete = chunk[: chunk.rfind(b"\n") + 1]
        text = complete.decode("utf-8", errors="replace")
        return {"lines": text.splitlines(), "offset": offset + len(complete)}

    def cancel(self, task_id: str) -> Optional[Dict[str, Any]]:
        before = self.repo.get_task(task_id)
        if not before:
            return None
        if before.status.is_terminal:
            return {"status": before.status.value, "cancelled": False,
                    "message": f"Задача уже завершена со статусом {before.status.value}; отмена не требуется."}
        task = self.cancel_fn(task_id)
        return {
            "status": task.status.value,
            "cancelled": task.status.value == "cancelled",
            "message": (
                "Отмена записана в базе данных. Если оркестратор запущен, работа агента остановится примерно "
                f"в течение {self.config.progress.poll_interval_sec} сек.; иначе проверьте агента вручную."
            ),
        }


def make_handler(app: DashboardApp, allowed_hosts: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentOrchestratorDashboard"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            logger.debug("%s - %s", self.address_string(), format % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_allowed(self) -> bool:
            # Blocks DNS-rebinding: a foreign page cannot reach us through its own hostname.
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if not self._host_allowed():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    page = PAGE_HTML.replace("__DASHBOARD_TOKEN__", app.token).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                    )
                    self.end_headers()
                    self.wfile.write(page)
                    return None
                if url.path == "/api/tasks":
                    return self._json(HTTPStatus.OK, app.list_tasks())
                match = _TASK_PATH.match(url.path)
                if match and match.group(2) is None:
                    detail = app.task_detail(match.group(1))
                    return self._json(HTTPStatus.OK, detail) if detail else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                if match and match.group(2) == "/log":
                    try:
                        offset = int(parse_qs(url.query).get("offset", ["0"])[0])
                    except ValueError:
                        offset = 0
                    tail = app.log_tail(match.group(1), offset)
                    return self._json(HTTPStatus.OK, tail) if tail else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except Exception as exc:  # never kill the server thread on one bad request
                logger.warning("Ошибка панели при обработке %s: %s", url.path, exc)
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if not self._host_allowed():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() not in allowed_hosts:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "origin not allowed"})
            if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), app.token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
            match = _TASK_PATH.match(urlparse(self.path).path)
            if not match or match.group(2) != "/cancel":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                result = app.cancel(match.group(1))
            except Exception as exc:
                logger.warning("Ошибка отмены из панели: %s", exc)
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
            return self._json(HTTPStatus.OK, result) if result else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def create_server(app: DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app, set()))
    bound_port = server.server_address[1]
    names = {host.lower()} | (LOOPBACK_HOSTS if host in LOOPBACK_HOSTS else set())
    allowed = {f"{name}:{bound_port}" for name in names} | {f"[{name}]:{bound_port}" for name in names if ":" in name}
    server.RequestHandlerClass = make_handler(app, allowed)
    server.daemon_threads = True
    return server
