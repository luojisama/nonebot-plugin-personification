"""WebUI QQ 空间路由：展示发空间月度额度/状态，并支持管理员手动发一条。

让 agent 自己按额度把控发不发，这里把额度状态可视化给管理员，并提供手动控制。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..deps import AdminIdentity, require_admin


def _first_bot(runtime) -> Any | None:
    try:
        bots = runtime.get_bots() if callable(getattr(runtime, "get_bots", None)) else {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


def _bundle_attr(runtime, name: str) -> Any:
    bundle = getattr(runtime, "runtime_bundle", None)
    return getattr(bundle, name, None) if bundle is not None else None


def _build_status(runtime) -> dict[str, Any]:
    from ...core.data_store import get_data_store
    from ...core.time_ctx import get_configured_now
    from ...jobs.periodic_jobs import build_qzone_quota

    cfg = getattr(runtime, "plugin_config", None)
    enabled = bool(getattr(cfg, "personification_qzone_enabled", False))
    proactive_enabled = bool(getattr(cfg, "personification_qzone_proactive_enabled", False))
    monthly_limit = int(getattr(cfg, "personification_qzone_monthly_limit", 30) or 30)
    min_interval_hours = float(getattr(cfg, "personification_qzone_min_interval_hours", 12.0) or 0)
    check_interval = int(getattr(cfg, "personification_qzone_check_interval", 60) or 60)
    quiet_start = int(getattr(cfg, "personification_qzone_quiet_hour_start", 0) or 0)
    quiet_end = int(getattr(cfg, "personification_qzone_quiet_hour_end", 7) or 7)

    state = get_data_store().load_sync("qzone_post_state")
    if not isinstance(state, dict):
        state = {}
    now = get_configured_now()
    quota = build_qzone_quota(
        state=state,
        now=now,
        monthly_limit=monthly_limit,
        min_interval_hours=min_interval_hours,
    )
    last_post_at = float(state.get("last_post_at", 0) or 0)
    next_eligible_at = float(quota.get("next_eligible_at", 0) or 0)
    now_ts = time.time()
    return {
        "enabled": enabled,
        "proactive_enabled": proactive_enabled,
        "publish_available": bool(_bundle_attr(runtime, "qzone_publish_available")),
        "quota": quota,
        "check_interval_minutes": check_interval,
        "quiet_hour_start": quiet_start,
        "quiet_hour_end": quiet_end,
        "last_post_at": last_post_at,
        "last_content": str(state.get("last_content", "") or ""),
        "next_eligible_at": next_eligible_at,
        "next_eligible_in_seconds": max(0, int(next_eligible_at - now_ts)) if next_eligible_at else 0,
        "recent_contents": [str(x) for x in list(state.get("recent_contents", []))][-12:],
        "server_now": int(now_ts),
    }


def build_qzone_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/qzone", tags=["qzone"])

    @router.get("/status")
    async def status(_: AdminIdentity = Depends(require_admin)) -> dict:
        return _build_status(runtime)

    @router.post("/post-now")
    async def post_now(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """管理员手动强制发一条说说（绕过额度/间隔/agent 决策，但仍计入月度额度）。"""
        from ...core.time_ctx import get_configured_now
        from ...jobs.periodic_jobs import build_qzone_quota, record_qzone_post

        logger = getattr(runtime, "logger", None)
        generate = _bundle_attr(runtime, "qzone_generate_post")
        publish = _bundle_attr(runtime, "publish_qzone_shuo")
        update_cookie = _bundle_attr(runtime, "update_qzone_cookie")
        if generate is None or publish is None:
            raise HTTPException(status_code=503, detail="发空间能力未就绪（qzone 未启用或运行时未初始化）")

        bot = _first_bot(runtime)
        if bot is None:
            raise HTTPException(status_code=503, detail="Bot 未连接")

        if callable(update_cookie):
            try:
                await update_cookie(bot)
            except Exception as exc:
                if logger is not None:
                    logger.warning(f"[webui] 手动发说说刷新 Cookie 失败：{exc}")

        try:
            content = await generate(bot)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"生成说说失败：{exc}") from exc
        if not content:
            return {"ok": False, "error": "本次没有生成出说说内容（可能被去重/审阅拦下），请稍后再试。"}

        try:
            success, msg = await publish(content, getattr(bot, "self_id", ""))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"发布失败：{exc}") from exc
        if not success:
            return {"ok": False, "error": f"发布失败：{msg}", "content": content[:2000]}

        state = record_qzone_post(content, now=get_configured_now())
        cfg = getattr(runtime, "plugin_config", None)
        quota = build_qzone_quota(
            state=state,
            now=get_configured_now(),
            monthly_limit=int(getattr(cfg, "personification_qzone_monthly_limit", 30) or 30),
            min_interval_hours=float(getattr(cfg, "personification_qzone_min_interval_hours", 12.0) or 0),
        )
        if logger is not None:
            logger.info(f"[webui] 管理员 {admin.qq} 手动发布了一条空间说说。")
        return {"ok": True, "content": content[:2000], "quota": quota}

    return router
