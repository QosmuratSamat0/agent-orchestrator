from __future__ import annotations

import collections
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent_orchestrator.bridge")


class BridgeError(Exception):
    pass


class BridgeClient:
    """Client for Agent-Bridge via stdio JSON-RPC MCP server protocol."""

    def __init__(self, command: str | List[str] = "agent-bridge", close_grace_sec: float = 5.0):
        self.command = command
        self.close_grace_sec = close_grace_sec
        self.restart_count = 0
        self._ever_started = False
        self.proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._stdout_queue: queue.Queue[Tuple[Optional[str], Optional[Exception]]] = queue.Queue()
        self._stderr_buffer: collections.deque[str] = collections.deque(maxlen=200)
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def _resolve_command(self) -> List[str]:
        if isinstance(self.command, list):
            return list(self.command)
        if isinstance(self.command, str):
            trimmed = self.command.strip()
            if " " in trimmed:
                return shlex.split(trimmed, posix=False)
            return [trimmed]
        return [str(self.command)]

    @staticmethod
    def _stdout_reader_worker(
        proc: subprocess.Popen,
        q: queue.Queue[Tuple[Optional[str], Optional[Exception]]],
        stop_ev: threading.Event,
    ) -> None:
        try:
            if proc.stdout is None:
                q.put((None, None))
                return
            for line in iter(proc.stdout.readline, ""):
                q.put((line, None))
        except Exception as e:
            q.put((None, e))
        finally:
            q.put((None, None))

    @staticmethod
    def _stderr_reader_worker(
        proc: subprocess.Popen,
        buf: collections.deque[str],
        stop_ev: threading.Event,
    ) -> None:
        try:
            if proc.stderr is None:
                return
            for line in iter(proc.stderr.readline, ""):
                buf.append(line.rstrip("\r\n")[:4000])
        except Exception:
            pass

    def get_stderr_tail(self) -> str:
        return "\n".join(self._stderr_buffer)

    def _cleanup_process(self) -> None:
        self._stop_event.set()
        proc = self.proc
        deadline = time.monotonic() + max(0.0, self.close_grace_sec) + 9.5

        def remaining(limit: float) -> float:
            return min(limit, max(0.0, deadline - time.monotonic()))

        if proc:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=max(0.0, self.close_grace_sec))
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    try:
                        killed = subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            capture_output=True, timeout=remaining(3), check=False,
                        )
                        if killed.returncode != 0:
                            logger.warning("Не удалось завершить дерево Agent-Bridge PID %s: %s", proc.pid,
                                           killed.stderr.decode("utf-8", errors="replace").strip())
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        logger.warning("Не удалось завершить дерево Agent-Bridge PID %s: %s", proc.pid, exc)
                    try:
                        proc.wait(timeout=remaining(5))
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                            proc.wait(timeout=remaining(1))
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            logger.warning("Не удалось завершить Agent-Bridge PID %s: %s", proc.pid, exc)
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=remaining(3))
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=remaining(2))
                    except subprocess.TimeoutExpired:
                        logger.warning("Не удалось завершить Agent-Bridge PID %s", proc.pid)
            except OSError as exc:
                logger.warning("Ошибка при ожидании Agent-Bridge PID %s: %s", proc.pid, exc)

        for thread, pipe in ((self._stdout_thread, proc.stdout if proc else None),
                             (self._stderr_thread, proc.stderr if proc else None)):
            if thread and thread.is_alive():
                thread.join(timeout=remaining(2))
            if thread and thread.is_alive():
                logger.warning("Поток чтения Agent-Bridge PID %s не завершился; pipe оставлен открытым",
                               proc.pid if proc else "?")
            elif pipe:
                try:
                    pipe.close()
                except Exception:
                    pass
        self.proc = None
        self._stdout_thread = None
        self._stderr_thread = None

        if hasattr(self, "_stdout_queue"):
            while not self._stdout_queue.empty():
                try:
                    self._stdout_queue.get_nowait()
                except Exception:
                    break

    def connect(self) -> None:
        with self._lock:
            self._start_server_locked()

    def reset(self) -> None:
        """Explicitly tears down the active connection and child process."""
        self.close()

    def reconnect(self) -> None:
        """Explicitly tears down and re-establishes connection to the MCP server."""
        with self._lock:
            self._cleanup_process()
            self._start_server_locked()

    def _start_server_locked(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if self.proc is not None:
            self._cleanup_process()

        cmd = self._resolve_command()
        logger.info("Запуск MCP-сервера Agent-Bridge: %s", cmd)
        self._stop_event.clear()
        self._stdout_queue = queue.Queue()
        self._stderr_buffer = collections.deque(maxlen=200)

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}),
            )
        except Exception as e:
            raise BridgeError(
                f"Failed to start Agent-Bridge process with command {cmd}: {e}"
            ) from e

        if self._ever_started:
            self.restart_count += 1
            logger.warning("Agent-Bridge перезапущен: задачи прежнего процесса больше недоступны для ожидания и отмены.")
        self._ever_started = True

        self._stdout_thread = threading.Thread(
            target=self._stdout_reader_worker,
            args=(self.proc, self._stdout_queue, self._stop_event),
            name="BridgeClient-StdoutReader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_worker,
            args=(self.proc, self._stderr_buffer, self._stop_event),
            name="BridgeClient-StderrReader",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        try:
            # 1. Initialize MCP
            init_payload = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent-orchestrator", "version": "0.1.0"},
            }
            resp = self._send_request_locked("initialize", init_payload, timeout=30.0)
            logger.debug(f"Agent-Bridge initialized: {resp}")

            # 2. Initialized notification
            self._send_notification_locked("notifications/initialized", {})
        except Exception:
            self._cleanup_process()
            raise

    def close(self) -> None:
        with self._lock:
            self._cleanup_process()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _send_request(
        self, method: str, params: Dict[str, Any], timeout: float = 300.0
    ) -> Dict[str, Any]:
        with self._lock:
            return self._send_request_locked(method, params, timeout=timeout)

    def _send_request_locked(
        self, method: str, params: Dict[str, Any], timeout: float = 300.0
    ) -> Dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            if method != "initialize":
                self._start_server_locked()
            else:
                if not self.proc or self.proc.poll() is not None:
                    code = self.proc.poll() if self.proc else None
                    stderr_tail = self.get_stderr_tail()
                    raise BridgeError(
                        f"Agent-Bridge process exited before initialize could be sent (code {code}): {stderr_tail}"
                    )

        req_id = self._next_id()
        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(req, ensure_ascii=False) + "\n"

        try:
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            stderr_tail = self.get_stderr_tail()
            code = self.proc.poll() if self.proc else None
            self._cleanup_process()
            raise BridgeError(
                f"Agent-Bridge stdin write failed (exit code {code}): {e}. Stderr:\n{stderr_tail}"
            ) from e

        start = time.time()
        while True:
            elapsed = time.time() - start
            remaining = timeout - elapsed
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for response to {method} (id={req_id}) after {timeout:.1f}s"
                )

            try:
                item = self._stdout_queue.get(timeout=min(max(0.01, remaining), 0.5))
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    code = self.proc.poll()
                    stderr_tail = self.get_stderr_tail()
                    self._cleanup_process()
                    raise BridgeError(
                        f"Agent-Bridge process exited unexpectedly (code {code}). Stderr:\n{stderr_tail}"
                    )
                continue

            line_content, err = item
            if err is not None:
                stderr_tail = self.get_stderr_tail()
                self._cleanup_process()
                raise BridgeError(
                    f"Error reading stdout from Agent-Bridge: {err}. Stderr:\n{stderr_tail}"
                ) from err

            if line_content is None:
                code = None
                if self.proc:
                    try:
                        code = self.proc.wait(timeout=1.0)
                    except Exception:
                        code = self.proc.poll()
                stderr_tail = self.get_stderr_tail()
                self._cleanup_process()
                raise BridgeError(
                    f"Agent-Bridge process closed stdout / exited unexpectedly (code {code}). Stderr:\n{stderr_tail}"
                )

            resp_line = line_content.strip()
            if not resp_line:
                continue

            try:
                data = json.loads(resp_line)
            except json.JSONDecodeError:
                logger.warning(f"Unrecognized non-JSON line from bridge: {resp_line}")
                continue

            if not isinstance(data, dict):
                logger.warning(f"Non-dict JSON received from bridge: {data}")
                continue

            if "method" in data:
                if "id" in data:
                    reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": data["id"]}
                    if data["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "Method not found"}
                    try:
                        assert self.proc is not None and self.proc.stdin is not None
                        self.proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
                        self.proc.stdin.flush()
                    except (BrokenPipeError, OSError) as exc:
                        raise BridgeError(f"Не удалось ответить на запрос Agent-Bridge: {exc}") from exc
                else:
                    logger.debug("Уведомление Agent-Bridge без ID: %s", data["method"])
                continue

            if data.get("id") == req_id:
                if "error" in data:
                    raise BridgeError(f"Bridge RPC error: {data['error']}")
                return data.get("result", {})
            else:
                logger.debug(
                    f"Discarding non-matching response id={data.get('id')} (waiting for req_id={req_id})"
                )

    def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        with self._lock:
            self._send_notification_locked(method, params)

    def _send_notification_locked(self, method: str, params: Dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            logger.warning(f"Failed to send notification {method}: {e}")

    def call_tool(
        self, name: str, arguments: Dict[str, Any], timeout: float = 300.0
    ) -> Dict[str, Any]:
        res = self._send_request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        if not isinstance(res, dict):
            return {"raw_response": res}

        # Check structured content or parse text content
        if "structuredContent" in res and isinstance(res["structuredContent"], dict):
            return res["structuredContent"]

        for item in res.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        if res.get("isError") and "ok" not in parsed:
                            parsed["ok"] = False
                        return parsed
                    return {"result": parsed}
                except Exception:
                    ret: Dict[str, Any] = {"raw_text": text}
                    if res.get("isError"):
                        ret["ok"] = False
                    return ret
        return res

    def list_agents(self) -> Dict[str, Any]:
        return self.call_tool("list_agents", {})

    def dispatch_task(
        self,
        agent: str,
        message: str,
        cwd: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        title: Optional[str] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "agent": agent,
            "message": message,
            "cwd": cwd,
            "user_requested": True,
        }
        if model:
            args["model"] = model
        if effort:
            args["effort"] = effort
        if title:
            args["title"] = title
        if request_id:
            args["request_id"] = request_id
        if session_id:
            args["session_id"] = session_id

        return self.call_tool("dispatch_task", args)

    def wait_task(self, task_id: str, timeout_sec: float = 180.0) -> Dict[str, Any]:
        rpc_timeout = timeout_sec + 30.0
        return self.call_tool(
            "wait_task", {"task_id": task_id, "timeout_sec": timeout_sec}, timeout=rpc_timeout
        )

    def check_task(self, task_id: str) -> Dict[str, Any]:
        return self.call_tool("check_task", {"task_id": task_id})

    def get_transcript(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
        kinds: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"session_id": session_id, "offset": offset, "limit": limit}
        if kinds is not None:
            args["kinds"] = kinds
        return self.call_tool("get_transcript", args)

    def get_result(
        self, task_id: str, max_chars: int = 60000
    ) -> Dict[str, Any]:
        full_text = []
        cursor = 0
        last_resp: Dict[str, Any] = {}
        iterations = 0
        max_iterations = 500

        while iterations < max_iterations:
            iterations += 1
            resp = self.call_tool(
                "get_result",
                {"task_id": task_id, "cursor": cursor, "max_chars": max_chars},
            )
            last_resp = resp
            if not resp.get("ok", False):
                return resp

            text_chunk = resp.get("result_text") or resp.get("text", "")
            if text_chunk:
                full_text.append(text_chunk)

            if not resp.get("has_more", False):
                break

            next_cursor = resp.get("next_cursor")
            if next_cursor is not None and next_cursor > cursor:
                cursor = next_cursor
            elif text_chunk:
                cursor += len(text_chunk)
            else:
                break

        result = dict(last_resp)
        if full_text:
            result["result_text"] = "".join(full_text)
        return result

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return self.call_tool("cancel_task", {"task_id": task_id})
