from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ...core import webui_audit_log
from ...core.operation_diagnostics import diagnostic, detail, step
from ...core.tool_creator import get_tool_creator_service
from ..deps import AdminIdentity, require_admin


def _error(exc: Exception, *, title: str) -> HTTPException:
    if isinstance(exc, KeyError):
        status, code = 404, "tool_creator_task_not_found"
    elif isinstance(exc, PermissionError):
        status, code = 403, "tool_creator_creator_required"
    elif isinstance(exc, ValueError):
        status, code = 400, "tool_creator_request_invalid"
    elif "version" in str(exc) or "changed" in str(exc) or "active" in str(exc):
        status, code = 409, "tool_creator_task_conflict"
    else:
        status, code = 500, "tool_creator_operation_failed"
    return HTTPException(
        status_code=status,
        detail=diagnostic(
            ok=False,
            code=code,
            phase="request" if status < 500 else "operation",
            title=title,
            message=str(exc) if status < 500 else "服务器未能完成该工具创建操作。",
            steps=(step("tool_creator_operation", title, "error", "操作未完成。"),),
            suggestion="刷新任务状态后重试。" if status == 409 else "检查输入或脱敏日志后重试。",
            retryable=status in {400, 409, 500},
        ),
    )


def build_tool_creator_router(*, runtime: Any) -> APIRouter:
    router = APIRouter(prefix="/api/tool-creator", tags=["tool-creator"])
    service = get_tool_creator_service(runtime)

    @router.get("/tasks")
    async def list_tasks(limit: int = Query(30, ge=1, le=100), _: AdminIdentity = Depends(require_admin)) -> dict:
        tasks = service.list(limit)
        return {"tasks": tasks, "total": len(tasks)}

    @router.get("/tasks/{task_id}")
    async def task_detail(task_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        task = service.get(task_id)
        if task is None:
            raise _error(KeyError(task_id), title="找不到创建工具任务")
        return {"task": task, "events": service.events(task_id)}

    @router.post("/tasks", status_code=202)
    async def create_task(body: dict = Body(default_factory=dict), admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            task = service.create(
                creator=admin.qq,
                request_text=str(body.get("request") or ""),
                suggested_name=str(body.get("suggested_name") or ""),
            )
        except Exception as exc:
            raise _error(exc, title="创建工具任务未建立") from exc
        webui_audit_log.record(action="tool_creator_create", qq=admin.qq, device_id=admin.device_id, target=task["task_id"], detail={"suggested_name": task.get("suggested_name", "")})
        return {"task": task}

    @router.post("/tasks/{task_id}/answer")
    async def answer(task_id: str, body: dict = Body(default_factory=dict), admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            task = service.answer(
                task_id=task_id,
                creator=admin.qq,
                question_id=str(body.get("question_id") or ""),
                expected_version=int(body.get("expected_version") or 0),
                answer=str(body.get("answer") or ""),
            )
        except Exception as exc:
            raise _error(exc, title="管理员回答未提交") from exc
        webui_audit_log.record(action="tool_creator_answer", qq=admin.qq, device_id=admin.device_id, target=task_id, detail={"question_id": str(body.get("question_id") or "")})
        return {"task": task}

    @router.post("/tasks/{task_id}/approve")
    async def approve(task_id: str, body: dict = Body(default_factory=dict), admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            task = await service.approve(
                task_id=task_id,
                creator=admin.qq,
                expected_version=int(body.get("expected_version") or 0),
                artifact_digest=str(body.get("artifact_digest") or ""),
            )
        except Exception as exc:
            webui_audit_log.record(action="tool_creator_approve", qq=admin.qq, device_id=admin.device_id, target=task_id, detail={"error_type": type(exc).__name__}, outcome="error")
            raise _error(exc, title="Skill 发布未完成") from exc
        webui_audit_log.record(action="tool_creator_approve", qq=admin.qq, device_id=admin.device_id, target=task_id, detail={"artifact_digest": task.get("artifact_digest", "")})
        report = diagnostic(
            ok=True,
            code="tool_creator_skill_published",
            phase="completed",
            title="Skill 已发布",
            message="声明式 Skill 已原子发布并进入当前 tool registry。",
            details=(detail("工具", task.get("context", {}).get("manifest", {}).get("name", ""), "ok"),),
            steps=(
                step("publish", "发布 Skill artifact", "ok", "artifact 已移动到 generated_skills。"),
                step("activate", "重载 tool registry", "ok", "新工具已在 live registry 中确认。"),
            ),
            operation_id=task_id,
        )
        return {"task": task, "diagnostic": report}

    @router.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str, body: dict = Body(default_factory=dict), admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            task = service.cancel(task_id=task_id, creator=admin.qq, expected_version=int(body.get("expected_version") or 0))
        except Exception as exc:
            raise _error(exc, title="创建工具任务未取消") from exc
        webui_audit_log.record(action="tool_creator_cancel", qq=admin.qq, device_id=admin.device_id, target=task_id)
        return {"task": task}

    @router.post("/tasks/{task_id}/retry")
    async def retry(task_id: str, body: dict = Body(default_factory=dict), admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            task = service.retry(task_id=task_id, creator=admin.qq, expected_version=int(body.get("expected_version") or 0))
        except Exception as exc:
            raise _error(exc, title="创建工具任务未重试") from exc
        webui_audit_log.record(action="tool_creator_retry", qq=admin.qq, device_id=admin.device_id, target=task_id)
        return {"task": task}

    return router
