from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core import webui_audit_log
from ..deps import AdminIdentity, require_admin


def build_audit_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/audit", tags=["audit"])

    @router.get("/recent")
    async def recent(
        limit: int = Query(default=100, ge=1, le=500),
        action: str = Query(default=""),
        qq: str = Query(default=""),
        before_ts: float = Query(default=0.0),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        rows = webui_audit_log.query_recent(
            limit=limit,
            action=action,
            qq=qq,
            before_ts=before_ts,
        )
        return {
            "entries": rows,
            "next_before_ts": rows[-1]["ts"] if rows else 0,
        }

    @router.get("/actions")
    async def action_catalog(_: AdminIdentity = Depends(require_admin)) -> dict:
        """已知 action 类型供前端筛选下拉用。"""
        return {
            "actions": [
                {"key": "login_code_sent", "label": "登录-发送验证码"},
                {"key": "login_verify", "label": "登录-提交验证码"},
                {"key": "device_revoke", "label": "撤销设备"},
                {"key": "config_update", "label": "修改配置"},
                {"key": "config_apply_recommended", "label": "应用推荐配置"},
                {"key": "sticker_delete", "label": "删除表情包"},
                {"key": "sticker_upload", "label": "上传表情包"},
                {"key": "sticker_rescan", "label": "重扫表情包"},
                {"key": "skill_toggle", "label": "Skill 启停"},
                {"key": "style_rebuild", "label": "群风格手动重建"},
            ],
        }

    return router
