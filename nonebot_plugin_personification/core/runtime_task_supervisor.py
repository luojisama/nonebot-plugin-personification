from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import metrics


@dataclass
class _TaskRecord:
    name: str
    task: asyncio.Task[Any]
    started_at: float
    finished_at: float = 0.0
    state: str = "running"
    error_code: str = ""


class RuntimeTaskSupervisor:
    """Own the small set of plugin startup/background support tasks."""

    def __init__(self) -> None:
        self._records: dict[str, _TaskRecord] = {}
        self._failed_total = 0
        self._logger: Any = None

    def configure(self, *, logger: Any = None) -> None:
        if logger is not None:
            self._logger = logger

    def start(self, name: str, factory: Callable[[], Awaitable[Any]]) -> asyncio.Task[Any]:
        normalized = str(name or "").strip() or "unnamed"
        current = self._records.get(normalized)
        if current is not None and not current.task.done():
            return current.task
        task = asyncio.create_task(factory(), name=f"personification:{normalized}")
        record = _TaskRecord(name=normalized, task=task, started_at=time.time())
        self._records[normalized] = record
        task.add_done_callback(lambda done, key=normalized: self._finish(key, done))
        return task

    def _finish(self, name: str, task: asyncio.Task[Any]) -> None:
        record = self._records.get(name)
        if record is None or record.task is not task:
            return
        record.finished_at = time.time()
        if task.cancelled():
            record.state = "cancelled"
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            record.state = "cancelled"
            return
        if error is None:
            record.state = "completed"
            return
        record.state = "failed"
        record.error_code = "runtime_task_failed"
        self._failed_total += 1
        metrics.record_counter("runtime_task_failed_total", task=name)
        logger = self._logger
        if logger is not None:
            try:
                logger.warning(
                    "[runtime] 后台任务失败 task=%s error_type=%s",
                    name,
                    type(error).__name__,
                )
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        items = []
        for name in sorted(self._records):
            record = self._records[name]
            finished_at = float(record.finished_at or 0.0)
            items.append(
                {
                    "name": record.name,
                    "state": record.state,
                    "error_code": record.error_code,
                    "started_at": record.started_at,
                    "finished_at": finished_at,
                    "duration_seconds": round(
                        max(0.0, (finished_at or now) - record.started_at),
                        3,
                    ),
                }
            )
        return {
            "total": len(items),
            "supervised": items,
            "failed_total": self._failed_total,
        }

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        active = [record.task for record in self._records.values() if not record.task.done()]
        for task in active:
            task.cancel()
        if active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active, return_exceptions=True),
                    timeout=max(0.1, float(timeout or 5.0)),
                )
            except asyncio.TimeoutError:
                metrics.record_counter("runtime_task_shutdown_timeout_total")

    def reset_for_testing(self) -> None:
        if any(not record.task.done() for record in self._records.values()):
            raise RuntimeError("cannot reset while supervised tasks are running")
        self._records.clear()
        self._failed_total = 0
        self._logger = None


runtime_task_supervisor = RuntimeTaskSupervisor()


__all__ = ["RuntimeTaskSupervisor", "runtime_task_supervisor"]
