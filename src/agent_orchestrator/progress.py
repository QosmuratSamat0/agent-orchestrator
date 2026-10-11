from __future__ import annotations

from datetime import datetime
import logging
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
            silent = f", тишина {resp['silent_for_sec']} с" if "silent_for_sec" in resp else ""
            self.log(task, f"статус {status}{silent}, прошло {int(tick - first)} с, осталось {remaining}")

            activity = resp.get("recent_activity")
            if isinstance(activity, list) and activity:
                newest = str(activity[-1])
                if newest != self._last_activity.get(task.id):
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
                    if not isinstance(event, dict):
                        continue
                    kind = str(event.get("type") or "событие")
                    data = event.get("data")
                    data = data if isinstance(data, dict) else {}
                    detail = next((data[key] for key in ("text", "title", "path", "summary") if data.get(key)), None)
                    message = f"{kind}: {detail}" if detail is not None else kind
                    message = " ".join(str(message).splitlines())[: self.max_line_chars]
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
