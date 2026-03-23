from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict

from ..agent.inner_state import get_personification_data_dir


def get_user_tasks_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_tasks.json"


def _load_user_tasks(data_dir: Path) -> dict:
    path = get_user_tasks_path(data_dir)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_user_tasks(data_dir: Path, payload: dict) -> None:
    path = get_user_tasks_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _job_id(user_id: str, task_id: str) -> str:
    return f"user_task_{user_id}_{task_id}"


def _parse_cron(cron_expr: str) -> dict:
    fields = cron_expr.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have 5 fields")
    minute, hour, day, month, day_of_week = fields
    return {
        "trigger": "cron",
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": day_of_week,
    }


def make_create_task_tool(
    scheduler: Any,
    data_dir: Path,
    bot_caller: Callable[[dict], Any] | None = None,
):
    async def _handler(
        user_id: str,
        description: str,
        cron: str,
        action: str,
        params: dict | None = None,
    ) -> str:
        payload = _load_user_tasks(data_dir)
        user_key = f"user_{user_id}"
        user_bucket = payload.setdefault(user_key, {"scheduled_tasks": []})
        task_list = user_bucket.setdefault("scheduled_tasks", [])
        task_id = f"task_{len(task_list) + 1:03d}"
        task = {
            "id": task_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "description": description,
            "cron": cron,
            "action": action,
            "params": params or {},
            "active": True,
            "last_executed": "",
        }
        task_list.append(task)
        _save_user_tasks(data_dir, payload)

        async def _job_wrapper() -> None:
            await _execute_task(task, bot_caller=bot_caller)

        scheduler.add_job(
            _job_wrapper,
            id=_job_id(user_id, task_id),
            replace_existing=True,
            **_parse_cron(cron),
        )
        return f"已创建任务 {task_id}"

    return _handler


def make_cancel_task_tool(scheduler: Any, data_dir: Path):
    async def _handler(user_id: str, task_id: str) -> str:
        payload = _load_user_tasks(data_dir)
        user_key = f"user_{user_id}"
        task_list = payload.get(user_key, {}).get("scheduled_tasks", [])
        for task in task_list:
            if task.get("id") == task_id:
                task["active"] = False
                _save_user_tasks(data_dir, payload)
                try:
                    scheduler.remove_job(_job_id(user_id, task_id))
                except Exception:
                    pass
                return f"已取消任务 {task_id}"
        return f"未找到任务 {task_id}"

    return _handler


async def _execute_task(task: dict, bot_caller: Callable[[dict], Any] | None) -> None:
    task["last_executed"] = time.strftime("%Y-%m-%d %H:%M")
    if bot_caller is not None:
        result = bot_caller(task)
        if asyncio.iscoroutine(result):
            await result


def restore_tasks_on_startup(scheduler: Any, data_dir: Path, bot_caller: Callable[[dict], Any]) -> None:
    payload = _load_user_tasks(data_dir)
    for user_key, user_data in payload.items():
        if not isinstance(user_data, dict):
            continue
        user_id = user_key.removeprefix("user_")
        for task in user_data.get("scheduled_tasks", []):
            if not isinstance(task, dict) or not task.get("active", False):
                continue

            async def _job_wrapper(task_ref=task) -> None:
                await _execute_task(task_ref, bot_caller)
                _save_user_tasks(data_dir, payload)

            scheduler.add_job(
                _job_wrapper,
                id=_job_id(user_id, str(task.get("id", ""))),
                replace_existing=True,
                **_parse_cron(str(task.get("cron", ""))),
            )


__all__ = [
    "get_personification_data_dir",
    "get_user_tasks_path",
    "make_cancel_task_tool",
    "make_create_task_tool",
    "restore_tasks_on_startup",
]
