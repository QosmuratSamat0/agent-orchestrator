"""Local stdio MCP tools for controlling the orchestrator from Codex."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, BinaryIO

from agent_orchestrator.config import OrchestratorConfig
from agent_orchestrator.models import TaskStatus
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.repository import ConcurrencyLimitError, TaskRepository


logger = logging.getLogger(__name__)
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", PROTOCOL_VERSION}
DEAD_OWNER_HINT = "Процесс-владелец не работает; вызовите recover_task или cli recover."
UNKNOWN_OWNER_HINT = "Владелец неизвестен; убедитесь, что задача не выполняется (cli list/watch), затем используйте cli recover."


def _worker_creation_flags(*, breakaway: bool = True) -> int:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return flags | subprocess.CREATE_BREAKAWAY_FROM_JOB if breakaway else flags


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied still means it exists.
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _owner_alive(task: Any, poll_interval_sec: float) -> bool:
    if not task.owner_pid or not task.heartbeat_at:
        return False
    try:
        heartbeat = datetime.fromisoformat(task.heartbeat_at.replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    except ValueError:
        return False
    return age < 3 * poll_interval_sec + 5 and _pid_alive(task.owner_pid)


def _task_summary(task: Any, poll_interval_sec: float | None = None) -> dict[str, Any]:
    summary = {
        "id": task.id,
        "title": task.title,
        "cwd": task.cwd,
        "status": task.status.value,
        "attempt": task.retry_count + 1,
        "verdict": task.verification_verdict.value if task.verification_verdict else None,
        "error": task.error_message,
    }
    if poll_interval_sec is not None and not task.status.is_terminal:
        if task.owner_pid is None:
            summary["owner_alive"] = None
            summary["hint"] = UNKNOWN_OWNER_HINT
        else:
            alive = _owner_alive(task, poll_interval_sec)
            summary["owner_alive"] = alive
            if not alive:
                summary["hint"] = DEAD_OWNER_HINT
    return summary


class OrchestratorTools:
    def __init__(self, config_path: str | None = None):
        if config_path and not Path(config_path).is_file():
            raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")
        selected = Path(config_path or os.environ.get("ORCHESTRATOR_CONFIG", "config.toml"))
        if not selected.is_file() and not config_path and Path("config.example.toml").is_file():
            selected = Path("config.example.toml")
        self.config_path = str(selected.resolve()) if selected.is_file() else None
        self.config = OrchestratorConfig.load(self.config_path)
        self.config.db_path = str(Path(self.config.db_path).resolve())
        self.repo = TaskRepository(self.config.db_path)

    def _spawn_worker(self, task_id: str, *, recover: bool = False) -> None:
        command = [sys.executable, "-m", "agent_orchestrator.worker", task_id,
                   "--db-path", self.config.db_path]
        if self.config_path:
            command.extend(["--config", self.config_path])
        if recover:
            command.append("--recover")
        log_dir = Path(self.config.progress.log_dir or Path(self.config.db_path).parent / "orchestrator_logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{task_id[:8]}.worker.log").open("ab") as log_file:
            options = {"stdin": subprocess.DEVNULL, "stdout": log_file,
                       "stderr": subprocess.STDOUT, "cwd": str(Path.cwd().resolve()),
                       "close_fds": True}
            if os.name == "nt":
                try:
                    process = subprocess.Popen(
                        command, creationflags=_worker_creation_flags(),
                        **options,
                    )
                except OSError as exc:
                    logger.warning("Не удалось отделить worker от job object: %s; повторяю без breakaway", exc)
                    process = subprocess.Popen(command, creationflags=_worker_creation_flags(breakaway=False), **options)
            else:
                process = subprocess.Popen(command, start_new_session=True, **options)
        self.repo.touch_owner(task_id, process.pid)

    def start_task(
        self, prompt: str, cwd: str, title: str = "", criteria: str = ""
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("Текст задачи не должен быть пустым.")
        if not os.path.isabs(cwd) or not Path(cwd).is_dir():
            raise ValueError("Укажите существующий абсолютный путь к проекту в cwd.")

        orchestrator = Orchestrator(config=self.config)
        try:
            task = orchestrator.create_task(
                title=title.strip() or prompt.strip()[:80],
                prompt=prompt,
                acceptance_criteria=criteria,
                cwd=cwd,
            )
        except ConcurrencyLimitError as exc:
            orchestrator.close()
            raise ValueError("Уже есть активная задача. Дождитесь её завершения.") from exc
        except Exception:
            orchestrator.close()
            raise

        try:
            self._spawn_worker(task.id)
        except Exception as exc:
            task.mark_failed(f"Не удалось запустить worker: {exc}")
            self.repo.update_task(task)
            self.repo.record_event(task.id, "TASK_FAILED", {"error": str(exc)})
            orchestrator._finish(task)
            raise RuntimeError(f"Не удалось запустить worker для задачи {task.id}: {exc}") from exc
        finally:
            orchestrator.close()
        return {**_task_summary(task), "message": "Задача запущена в отдельном процессе. Проверяйте статус по ID."}

    def get_status(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise ValueError("Задача не найдена.")
        return _task_summary(task, self.config.progress.poll_interval_sec)

    def list_tasks(self, limit: int = 10) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("Лимит должен быть от 1 до 50.")
        return {"tasks": [_task_summary(t, self.config.progress.poll_interval_sec)
                          for t in self.repo.list_tasks(limit=limit)]}

    def recover_task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise ValueError("Задача не найдена.")
        if task.status.is_terminal:
            return _task_summary(task)
        if task.owner_pid is None:
            raise ValueError(UNKNOWN_OWNER_HINT)
        if _owner_alive(task, self.config.progress.poll_interval_sec):
            raise ValueError("Процесс-владелец задачи ещё работает; восстановление запрещено.")
        try:
            self._spawn_worker(task_id, recover=True)
        except Exception as exc:
            raise RuntimeError(f"Не удалось запустить восстановление задачи {task_id}: {exc}") from exc
        return {**_task_summary(task), "message": "Восстановление запущено в фоне. Проверяйте результат через get_status."}

    def get_report(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise ValueError("Задача не найдена.")
        path = Path(task.cwd) / f"orchestrator_report_{task.id[:8]}.md"
        if not path.is_file():
            return {"task": _task_summary(task), "message": "Отчёт ещё не готов."}
        content = path.read_text(encoding="utf-8")
        return {"task": _task_summary(task), "path": str(path), "report": content}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        orchestrator = Orchestrator(config=self.config)
        try:
            task = orchestrator.cancel_task(task_id, notify_bridge=False)
            message = (
                "Отмена записана. Запущенный оркестратор остановит работу при следующей проверке."
                if task.status.value == "cancelled"
                else "Задача уже завершена; отмена не требуется."
            )
            return {
                **_task_summary(task),
                "message": message,
            }
        finally:
            orchestrator.close()


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("start_task", "Запустить задачу в проекте; сразу вернуть её ID.", {
        "prompt": {"type": "string", "description": "Что нужно сделать"},
        "cwd": {"type": "string", "description": "Абсолютный путь к целевому проекту"},
        "title": {"type": "string", "description": "Короткое название задачи"},
        "criteria": {"type": "string", "description": "Критерии приёмки"},
    }, ["prompt", "cwd"]),
    _tool("get_status", "Получить текущий статус задачи по ID.", {
        "task_id": {"type": "string"},
    }, ["task_id"]),
    _tool("list_tasks", "Показать последние задачи.", {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    }, []),
    _tool("get_report", "Прочитать итоговый отчёт задачи по ID.", {
        "task_id": {"type": "string"},
    }, ["task_id"]),
    _tool("recover_task", "Восстановить задачу после остановки её процесса без повторной отправки агенту.", {
        "task_id": {"type": "string"},
    }, ["task_id"]),
    _tool("cancel_task", "Записать отмену задачи без запуска нового Agent-Bridge.", {
        "task_id": {"type": "string"},
    }, ["task_id"]),
]


class MCPServer:
    def __init__(self, tools: OrchestratorTools, reader: BinaryIO, writer: BinaryIO):
        self.tools = tools
        self.reader = reader
        self.writer = writer

    def _write(self, response: dict[str, Any]) -> None:
        self.writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        self.writer.flush()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in request:
            return None  # MCP notifications have no reply.
        request_id = request["id"]
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                client_version = params.get("protocolVersion")
                result = {
                    "protocolVersion": client_version if client_version in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-orchestrator", "version": "0.1.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                if name not in {tool["name"] for tool in TOOLS}:
                    raise ValueError("Неизвестный инструмент.")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise ValueError("Аргументы инструмента должны быть объектом.")
                try:
                    value = getattr(self.tools, name)(**arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                except Exception as exc:
                    logger.exception("Ошибка инструмента %s", name)
                    result = {"content": [{"type": "text", "text": f"Ошибка: {exc}"}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": "Метод не найден."}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": f"Неверный запрос: {exc}"}}

    def serve(self) -> None:
        for line in self.reader:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Некорректный JSON."}})
                continue
            if not isinstance(request, dict):
                self._write({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": "Некорректный запрос."}})
                continue
            if "method" not in request:
                continue
            response = self.handle(request)
            if response is not None:
                self._write(response)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Локальный MCP-сервер Agent Orchestrator")
    parser.add_argument("--config", help="Путь к конфигурации оркестратора")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    MCPServer(OrchestratorTools(args.config), sys.stdin.buffer, sys.stdout.buffer).serve()


if __name__ == "__main__":
    main()
