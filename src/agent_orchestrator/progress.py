from __future__ import annotations

from datetime import datetime
import logging
import re
from pathlib import Path
import time
from typing import Any, Callable

from agent_orchestrator.models import Task


logger = logging.getLogger("agent_orchestrator.progress")


class ProgressFeed:
    def __init__(
        self,
        log_dir: Path,
        poll_interval_sec: float = 10.0,
        echo: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_line_chars: int = 300,
        enabled: bool = True,
    ):
        self.log_dir = Path(log_dir)
        self.poll_interval_sec = poll_interval_sec
        self.echo = echo
        self.clock = clock
        self.max_line_chars = max_line_chars
        self.enabled = enabled
        self._first_seen: dict[str, float] = {}
        self._last_tick: dict[str, float] = {}
        self._last_status: dict[str, str] = {}
        self._last_activity: dict[str, str] = {}
        self._offsets: dict[str, int] = {}
        self._last_transcript: dict[str, str] = {}

    def log(self, task: Task, text: str) -> None:
        if not self.enabled:
            return
        try:
            message = " ".join(str(text).splitlines())
            line = f"[{datetime.now().strftime('%H:%M:%S')}] попытка {task.retry_count + 1} | {message}"
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / f"{task.id[:8]}.log").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
            if self.echo:
                self.echo(line)
        except Exception:
            logger.debug("Не удалось записать прогресс задачи", exc_info=True)

    def on_wait_progress(
        self,
        task: Task,
        bridge: Any,
        resp: dict[str, Any],
        deadline_epoch: float | None = None,
        now: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            tick = self.clock()
            status = str(resp.get("status") or "неизвестен")
            first = self._first_seen.setdefault(task.id, tick)
            last = self._last_tick.get(task.id)
            if last is not None and status == self._last_status.get(task.id) and tick - last < self.poll_interval_sec:
                return
            self._last_tick[task.id] = tick
            self._last_status[task.id] = status

            remaining = "нет"
            if deadline_epoch is not None:
                seconds = max(0, int(deadline_epoch - (now if now is not None else time.time())))
                remaining = f"{seconds // 60:02}:{seconds % 60:02}"
            silent = f", тишина {resp['silent_for_sec']} с" if resp.get("silent_for_sec") is not None else ""
            self.log(task, f"статус {status}{silent}, прошло {int(tick - first)} с, осталось {remaining}")

            activity = resp.get("recent_activity")
            if isinstance(activity, list) and activity:
                newest = str(activity[-1])
                # Bare event kinds ("raw", "tool_call") carry no information; the transcript tail shows the details.
                if not re.fullmatch(r"[a-z_]+", newest) and newest != self._last_activity.get(task.id):
                    self._last_activity[task.id] = newest
                    self.log(task, f"активность: {newest}")

            session_id = task.bridge_session_id
            if not session_id:
                return
            for _ in range(3):
                offset = self._offsets.get(session_id, 0)
                page = bridge.get_transcript(session_id, offset=offset, limit=50)
                if not isinstance(page, dict) or not page.get("ok", True):
                    break
                events = page.get("events")
                if not isinstance(events, list) or not events:
                    break
                for event in events:
                    message = describe_transcript_event(event)
                    if message is None:
                        continue
                    message = " ".join(str(message).splitlines())[: self.max_line_chars]
                    # Antigravity repeats identical step updates; show each distinct line once in a row.
                    if message == self._last_transcript.get(task.id):
                        continue
                    self._last_transcript[task.id] = message
                    self.log(task, message)
                next_offset = page.get("next_offset")
                self._offsets[session_id] = (
                    next_offset if isinstance(next_offset, int) and not isinstance(next_offset, bool)
                    else offset + len(events)
                )
                if self._offsets[session_id] <= offset:
                    break
        except Exception:
            logger.debug("Не удалось получить прогресс задачи", exc_info=True)


_PARAM_KEYS = ("AbsolutePath", "TargetFile", "path", "file", "CommandLine", "command", "Query", "query")


def describe_transcript_event(event: Any) -> str | None:
    """One readable line for an Agent-Bridge transcript event, or None when it carries nothing useful."""
    if not isinstance(event, dict):
        return None
    kind = str(event.get("type") or "событие")
    data = event.get("data")
    data = data if isinstance(data, dict) else {}
    detail = next((data[key] for key in ("text", "title", "path", "summary") if data.get(key)), None)
    if detail is not None:
        return f"{kind}: {detail}"
    if kind == "turn_end":
        return f"конец хода: {data.get('stop_reason') or 'без причины'}"

    payload = data.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("event") == "init":
        init = payload.get("init") if isinstance(payload.get("init"), dict) else {}
        return f"старт агента: модель {init.get('model') or 'неизвестна'}"
    step = payload.get("step_update")
    if isinstance(step, dict):
        state = str(step.get("state") or "")
        if step.get("step_type") == "tool":
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            name = step.get("tool_name") or info.get("name") or "инструмент"
            params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
            target = next((str(params[key]) for key in _PARAM_KEYS if params.get(key)), "")
            line = f"инструмент {name} {target}".rstrip() + f" [{state}]"
            output_lines = [part.strip(" =") for part in str(info.get("output") or "").splitlines() if part.strip(" =")]
            if output_lines and state == "DONE":
                line += f" → {output_lines[-1][:160]}"
            return line
        if step.get("step_type") == "agent_response":
            return f"ответ агента [{state}]"
        return f"шаг {step.get('step_type') or kind} [{state}]"
    if kind == "raw":
        return None
    return kind
