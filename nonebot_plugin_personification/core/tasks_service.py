from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from .db import connect_sync


def get_user_tasks_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_tasks.json"


def _job_id(user_id: str, task_id: str) -> str:
    return f"user_task_{user_id}_{task_id}"


def parse_cron(cron_expr: str) -> dict:
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


def _list_user_tasks(user_id: str) -> list[dict[str, Any]]:
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT task_id, description, cron, action, params, active, created_at,
                   last_executed_at, last_status
            FROM user_tasks
            WHERE user_id=?
            ORDER BY task_id ASC
            """,
            (user_id,),
        ).fetchall()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        tasks.append(
            {
                "id": row["task_id"],
                "description": row["description"],
                "cron": row["cron"],
                "action": row["action"],
                "params": params if isinstance(params, dict) else {},
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "last_executed_at": row["last_executed_at"],
                "last_status": row["last_status"],
            }
        )
    return tasks


def _save_task(task: dict[str, Any], user_id: str) -> None:
    with connect_sync() as conn:
        conn.execute(
            """
            INSERT INTO user_tasks(
                task_id, user_id, description, cron, action, params, active,
                created_at, last_executed_at, last_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, task_id) DO UPDATE SET
                description=excluded.description,
                cron=excluded.cron,
                action=excluded.action,
                params=excluded.params,
                active=excluded.active,
                created_at=excluded.created_at,
                last_executed_at=excluded.last_executed_at,
                last_status=excluded.last_status
            """,
            (
                str(task["id"]),
                str(user_id),
                str(task.get("description", "") or ""),
                str(task.get("cron", "") or ""),
                str(task.get("action", "") or ""),
                json.dumps(task.get("params", {}), ensure_ascii=False),
                1 if bool(task.get("active", True)) else 0,
                float(task.get("created_at", time.time()) or time.time()),
                task.get("last_executed_at"),
                str(task.get("last_status", "pending") or "pending"),
            ),
        )
        conn.commit()


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
        existing = _list_user_tasks(str(user_id))
        task_id = f"task_{len(existing) + 1:03d}"
        task = {
            "id": task_id,
            "description": description,
            "cron": cron,
            "action": action,
            "params": params or {},
            "active": True,
            "created_at": time.time(),
            "last_executed_at": None,
            "last_status": "pending",
            "user_id": str(user_id),
        }
        _save_task(task, str(user_id))

        async def _job_wrapper() -> None:
            await _execute_task(task, bot_caller=bot_caller)

        scheduler.add_job(
            _job_wrapper,
            id=_job_id(str(user_id), task_id),
            replace_existing=True,
            **parse_cron(cron),
        )
        return f"已创建任务 {task_id}"

    return _handler


def make_cancel_task_tool(scheduler: Any, data_dir: Path):
    async def _handler(user_id: str, task_id: str) -> str:
        tasks = _list_user_tasks(str(user_id))
        for task in tasks:
            if task.get("id") != task_id:
                continue
            task["active"] = False
            _save_task(task, str(user_id))
            try:
                scheduler.remove_job(_job_id(str(user_id), task_id))
            except Exception:
                pass
            return f"已取消任务 {task_id}"
        return f"未找到任务 {task_id}"

    return _handler


async def _execute_task(task: dict, bot_caller: Callable[[dict], Any] | None) -> None:
    task["last_executed_at"] = time.time()
    task["last_status"] = "sent"
    if bot_caller is not None:
        try:
            result = bot_caller(task)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            task["last_status"] = "failed"
            raise
    _save_task(task, str(task.get("user_id", "") or ""))


def restore_tasks_on_startup(scheduler: Any, data_dir: Path, bot_caller: Callable[[dict], Any]) -> None:
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT task_id, user_id, description, cron, action, params, active,
                   created_at, last_executed_at, last_status
            FROM user_tasks
            WHERE active=1
            ORDER BY user_id, task_id
            """
        ).fetchall()

    for row in rows:
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        task = {
            "id": row["task_id"],
            "user_id": row["user_id"],
            "description": row["description"],
            "cron": row["cron"],
            "action": row["action"],
            "params": params if isinstance(params, dict) else {},
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "last_executed_at": row["last_executed_at"],
            "last_status": row["last_status"],
        }

        async def _job_wrapper(task_ref=task) -> None:
            await _execute_task(task_ref, bot_caller)

        scheduler.add_job(
            _job_wrapper,
            id=_job_id(str(task["user_id"]), str(task["id"])),
            replace_existing=True,
            **parse_cron(str(task.get("cron", ""))),
        )
