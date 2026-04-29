from __future__ import annotations

import json
import time
from pathlib import Path

from plugin.personification.core import tasks_service as service


DATA_DIR = Path("data") / "personification"


def _load_payload() -> dict:
    path = service.get_user_tasks_path(DATA_DIR)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_payload(payload: dict) -> None:
    path = service.get_user_tasks_path(DATA_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_create(user_id: str, description: str, cron: str, action: str, params: dict | None = None) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        return "缺少 user_id。"
    if not str(description or "").strip():
        return "缺少 description。"
    if not str(cron or "").strip():
        return "缺少 cron。"
    try:
        service.parse_cron(str(cron).strip())
    except Exception as e:
        return f"cron 格式无效: {e}"

    payload = _load_payload()
    user_key = f"user_{uid}"
    bucket = payload.setdefault(user_key, {"scheduled_tasks": []})
    tasks = bucket.setdefault("scheduled_tasks", [])
    task_id = f"task_{len(tasks) + 1:03d}"
    tasks.append(
        {
            "id": task_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "description": str(description).strip(),
            "cron": str(cron).strip(),
            "action": str(action or "remind").strip(),
            "params": params or {},
            "active": True,
            "last_executed": "",
        }
    )
    _save_payload(payload)
    return f"已创建任务 {task_id}"


async def run_cancel(user_id: str, task_id: str) -> str:
    uid = str(user_id or "").strip()
    tid = str(task_id or "").strip()
    if not uid or not tid:
        return "缺少 user_id 或 task_id。"
    payload = _load_payload()
    user_key = f"user_{uid}"
    task_list = payload.get(user_key, {}).get("scheduled_tasks", [])
    for task in task_list:
        if str(task.get("id")) == tid:
            task["active"] = False
            _save_payload(payload)
            return f"已取消任务 {tid}"
    return f"未找到任务 {tid}"


async def run(operation: str, user_id: str, task_id: str = "", description: str = "", cron: str = "", action: str = "remind", params: dict | None = None) -> str:
    op = str(operation or "").strip().lower()
    if op in {"create", "add"}:
        return await run_create(user_id=user_id, description=description, cron=cron, action=action, params=params)
    if op in {"cancel", "remove", "delete"}:
        return await run_cancel(user_id=user_id, task_id=task_id)
    return "operation 可选: create, cancel"
