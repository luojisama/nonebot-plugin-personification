from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core import webui_audit_log
from ...core.onebot_cache import get_user_nickname, get_user_profile
from ...core.operation_diagnostics import detail as operation_detail
from ...core.operation_diagnostics import diagnostic, exception_diagnostic, step
from ...core.user_profile_meta import compact_user_profile_meta_summary
from ...core.user_avatar_insight import (
    clear_user_avatar_analysis,
    force_user_avatar_analysis,
    get_user_avatar_state,
)
from ..deps import AdminIdentity, require_admin
from .favorability_view import serialize_favorability


_PUBLIC_QQ_PROFILE_FIELDS = frozenset(
    {
        "user_id",
        "avatar_url",
        "homepage_url",
        "updated_at",
        "source",
        "nickname",
        "remark",
        "card",
        "sex",
        "age",
        "qid",
        "level",
        "login_days",
        "signature",
        "area",
        "role",
        "title",
        "title_expire_time",
        "vip_level",
        "qq_level",
        "constellation",
    }
)


def _profile_service(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "profile_service", None)


def _persona_store(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "persona_store", None)


def _scoped_profile_service(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "scoped_profile_service", None)


def _get_first_bot(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    get_bots = getattr(bundle, "get_bots", None)
    if not callable(get_bots):
        return None
    try:
        bots = get_bots() or {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


def _get_bot_by_id(runtime: Any, bot_id: str) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    get_bots = getattr(bundle, "get_bots", None) if bundle is not None else None
    if not callable(get_bots):
        return None
    try:
        bots = get_bots() or {}
    except Exception:
        return None
    values = bots.values() if isinstance(bots, dict) else []
    return next(
        (
            bot
            for bot in values
            if str(getattr(bot, "self_id", "") or "").strip() == str(bot_id)
        ),
        None,
    )


def _runtime_bundle(runtime) -> Any | None:
    return getattr(runtime, "runtime_bundle", None)


def _record_avatar_audit(runtime: Any, **payload: Any) -> bool:
    try:
        webui_audit_log.record(**payload)
        return True
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(f"[avatar analysis operation] audit_failed type={type(exc).__name__}")
        return False


def _public_profile_json(profile_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    public_json = dict(profile_json if isinstance(profile_json, dict) else {})
    raw_qq_profile = public_json.get("qq_profile")
    if not isinstance(raw_qq_profile, dict):
        public_json.pop("qq_profile", None)
        return public_json, {}
    public_qq_profile = {
        key: value
        for key, value in raw_qq_profile.items()
        if key in _PUBLIC_QQ_PROFILE_FIELDS
    }
    public_json["qq_profile"] = public_qq_profile
    return public_json, public_qq_profile


def _merge_display_profile(stored: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    merged = dict(stored or {})
    for key, value in (live or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def build_persona_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/personas", tags=["personas"])

    @router.get("")
    async def list_core(_: AdminIdentity = Depends(require_admin)) -> dict:
        svc = _profile_service(runtime)
        if svc is None:
            return {"profiles": [], "available": False}
        profiles = svc.list_core_profiles()
        bot = _get_first_bot(runtime)
        items: list[dict[str, Any]] = []
        for p in profiles:
            uid = p["user_id"]
            profile_json = p.get("profile_json", {}) if isinstance(p.get("profile_json", {}), dict) else {}
            stored_qq_profile = profile_json.get("qq_profile", {}) if isinstance(profile_json.get("qq_profile", {}), dict) else {}
            live_qq_profile = await get_user_profile(bot, uid)
            _public_json, qq_profile = _public_profile_json(
                {"qq_profile": _merge_display_profile(stored_qq_profile, live_qq_profile)}
            )
            nickname = str(qq_profile.get("nickname", "") or "") or await get_user_nickname(bot, uid)
            snippet = (p["profile_text"] or "")[:140] or compact_user_profile_meta_summary(qq_profile)
            items.append(
                {
                    "user_id": uid,
                    "nickname": nickname,
                    "avatar_url": str(qq_profile.get("avatar_url", "") or ""),
                    "homepage_url": str(qq_profile.get("homepage_url", "") or ""),
                    "qq_profile": qq_profile,
                    "snippet": snippet,
                    "updated_at": p.get("updated_at", 0),
                    "source": p.get("source", ""),
                    "favorability": serialize_favorability(
                        runtime,
                        str(uid),
                        scope="user",
                        include_events=False,
                    ),
                }
            )
        return {"profiles": items, "available": True}

    @router.get("/{user_id}")
    async def detail(
        user_id: str,
        _: AdminIdentity = Depends(require_admin),
        group_id: str = "",
    ) -> dict:
        svc = _profile_service(runtime)
        if svc is None:
            raise HTTPException(status_code=503, detail="profile_service 未就绪")
        group_id = str(group_id or "").strip()
        if group_id and (not group_id.isdigit() or int(group_id) <= 0):
            raise HTTPException(status_code=400, detail="group_id 无效")
        core = svc.get_core_profile(user_id)
        groups = svc.list_groups()
        local_profiles: list[dict[str, Any]] = []
        for gid in groups:
            entry = svc.get_local_profile(group_id=gid, user_id=user_id)
            if entry is None:
                continue
            local_profiles.append(
                {
                    "group_id": gid,
                    "profile_text": entry.profile_text,
                    "profile_json": entry.profile_json,
                    "updated_at": entry.updated_at,
                    "effective_claims": svc.get_effective_claims(
                        user_id=user_id,
                        group_id=gid,
                    ),
                    "effective_source": (
                        "group_delta"
                        if isinstance(entry.profile_json, dict)
                        and entry.profile_json.get("schema_version") == 2
                        and bool(entry.profile_json.get("claims"))
                        else "legacy_local"
                    ),
                }
            )
        core_json = (core.profile_json if core and isinstance(core.profile_json, dict) else {}) or {}
        stored_qq_profile = core_json.get("qq_profile", {}) if isinstance(core_json.get("qq_profile", {}), dict) else {}
        live_qq_profile = await get_user_profile(_get_first_bot(runtime), user_id)
        qq_profile = _merge_display_profile(stored_qq_profile, live_qq_profile)
        public_core_json, qq_profile = _public_profile_json({**core_json, "qq_profile": qq_profile})
        avatar_state = get_user_avatar_state(svc, user_id, include_hash=False)
        structured = core_json.get("structured") or {}
        if not structured and core:
            # 旧画像没有结构化字段时，即时解析一次（不写库）
            try:
                from ...core.persona_service import parse_persona_structured

                structured = parse_persona_structured(core.profile_text or "")
            except Exception:
                structured = {}
        return {
            "user_id": user_id,
            "favorability": serialize_favorability(
                runtime,
                str(user_id),
                scope="user",
                include_events=True,
            ),
            "core_profile": {
                "profile_text": core.profile_text if core else "",
                "profile_json": public_core_json,
                "structured": structured,
                "qq_profile": qq_profile,
                "user_corrections": core_json.get("user_corrections", {}),
                "avatar_analysis": avatar_state.get("analysis", {}),
                "avatar_insight": avatar_state.get("insight", {}),
                "updated_at": core.updated_at if core else 0,
            } if core else {
                "profile_text": "",
                "profile_json": {},
                "structured": {},
                "qq_profile": qq_profile,
                "user_corrections": {},
                "avatar_analysis": avatar_state.get("analysis", {}),
                "avatar_insight": avatar_state.get("insight", {}),
                "updated_at": 0,
            },
            "local_profiles": local_profiles,
            "effective_claims": svc.get_effective_claims(
                user_id=user_id,
                group_id=group_id,
            ),
            "effective_scope": (
                {"kind": "group", "group_id": group_id}
                if str(group_id or "").strip()
                else {"kind": "global"}
            ),
            "prompt_block": svc.build_prompt_block(
                user_id=user_id,
                group_id=group_id,
            ),
        }

    @router.post("/{user_id}/group-refresh")
    async def refresh_group_profile(
        user_id: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        group_id = str(body.get("group_id", "") or "").strip()
        bot_id = str(body.get("bot_id", "") or "").strip()

        def _audit(outcome: str, code: str, detail: dict[str, Any] | None = None) -> bool:
            try:
                webui_audit_log.record(
                    action="persona_group_refresh",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=f"{bot_id or 'unspecified'}:{group_id or 'invalid'}:{user_id}",
                    detail={"bot_id": bot_id, "code": code, **(detail or {})},
                    outcome=outcome,
                )
                return True
            except Exception as exc:
                logger = getattr(runtime, "logger", None)
                if logger is not None:
                    logger.warning(
                        f"[persona group refresh] audit_failed type={type(exc).__name__}"
                    )
                return False

        service = _scoped_profile_service(runtime)
        if service is None:
            _audit("error", "service_unavailable")
            raise HTTPException(status_code=503, detail="scoped profile service 未就绪")
        if not group_id.isdigit() or int(group_id) <= 0:
            _audit("denied", "invalid_group_id")
            raise HTTPException(status_code=400, detail="group_id 无效")
        if not str(user_id or "").isdigit() or int(user_id) <= 0:
            _audit("denied", "invalid_user_id")
            raise HTTPException(status_code=400, detail="user_id 无效")
        if not bot_id:
            _audit("denied", "bot_id_required")
            raise HTTPException(status_code=400, detail="必须显式提供 bot_id")
        bot = _get_bot_by_id(runtime, bot_id)
        if bot is None:
            _audit("denied", "bot_offline")
            raise HTTPException(status_code=404, detail="指定 Bot 当前不在线")
        if not callable(getattr(bot, "get_group_list", None)):
            _audit("denied", "membership_unavailable")
            raise HTTPException(status_code=403, detail="无法确认指定 Bot 的群 membership")
        try:
            groups = await bot.get_group_list()
        except Exception as exc:
            _audit("denied", "bot_group_membership_failed")
            raise HTTPException(status_code=403, detail="指定 Bot 群 membership 检查失败") from exc
        if not any(
            str(item.get("group_id", "") or "") == group_id
            for item in (groups or [])
            if isinstance(item, dict)
        ):
            _audit("denied", "bot_not_in_group")
            raise HTTPException(status_code=403, detail="指定 Bot 不在目标群")
        try:
            member = await bot.get_group_member_info(
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
        except Exception as exc:
            _audit("denied", "user_membership_failed")
            raise HTTPException(
                status_code=403,
                detail="无法确认目标用户属于该群，已拒绝 scoped refresh",
            ) from exc
        if (
            not isinstance(member, dict)
            or str(member.get("user_id", "") or "") != str(user_id)
            or str(member.get("group_id", "") or "") != group_id
            or str(getattr(bot, "self_id", "") or "") != bot_id
        ):
            _audit("denied", "membership_tuple_mismatch")
            raise HTTPException(
                status_code=403,
                detail="目标用户群成员身份未得到协议确认",
            )
        try:
            result = await service.refresh_group_profile(
                group_id=group_id,
                user_id=user_id,
                force=True,
            )
        except Exception as exc:
            _audit("error", "refresh_failed", {"error_type": type(exc).__name__})
            raise HTTPException(status_code=500, detail="scoped profile 刷新失败") from exc
        audit_ok = _audit(
            "ok" if result.status == "succeeded" else result.status,
            result.code,
            {"status": result.status},
        )
        return {
            "status": result.status,
            "code": result.code,
            "bot_id": bot_id,
            "group_id": result.group_id,
            "user_id": result.user_id,
            "revision": result.revision,
            "claim_count": result.claim_count,
            "anchor_count": result.anchor_count,
            "partial": not audit_ok,
            "warnings": (
                []
                if audit_ok
                else ["scoped profile 刷新结果已产生，但管理员审计记录写入失败。"]
            ),
        }

    @router.post("/{user_id}/correction")
    async def correction(
        user_id: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """用户/管理员更正画像：以最高优先级写入并保留到后续再生成。"""
        store = _persona_store(runtime)
        if store is None or not hasattr(store, "apply_user_correction"):
            report = diagnostic(
                ok=False,
                code="persona_correction_unavailable",
                phase="precondition",
                title="画像更正暂不可用",
                message="persona_store 未就绪，尚未执行画像写入。",
                details=(operation_detail("目标用户", user_id),),
                steps=(
                    step("precondition", "检查画像服务", "error", "画像服务未就绪。"),
                    step("persist", "保存画像更正", "skipped", "未执行任何写入。"),
                ),
                suggestion="等待插件运行时完成初始化后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
            raise HTTPException(status_code=503, detail=report)
        corrections = body.get("corrections") or {}
        if not isinstance(corrections, dict) or not corrections:
            report = diagnostic(
                ok=False,
                code="persona_correction_invalid",
                phase="request_validation",
                title="画像更正未提交",
                message="corrections 必须是非空对象。",
                details=(operation_detail("目标用户", user_id),),
                steps=(
                    step("validate", "校验画像更正", "error", "没有可保存的更正字段。"),
                    step("persist", "保存画像更正", "skipped", "未执行任何写入。"),
                ),
                suggestion="填写至少一个字段及其更正值后重新提交。",
                retryable=False,
                partial=False,
                outcome_unknown=False,
            )
            raise HTTPException(status_code=400, detail=report)
        try:
            entry = await store.apply_user_correction(user_id, corrections)
        except Exception as exc:
            report = exception_diagnostic(
                exc,
                phase="persistence",
                title="画像更正未完成",
                message="服务器未能确认画像更正已完整保存。",
                suggestion="先刷新该用户画像确认当前字段；未生效时再提交更正。",
                retryable=False,
            )
            report["code"] = "persona_correction_persist_failed"
            report["details"] = [
                operation_detail("目标用户", user_id).to_dict(),
                operation_detail("更正字段", list(corrections.keys())).to_dict(),
                *report.get("details", []),
            ]
            report["steps"] = [
                step("validate", "校验画像更正", "ok", "更正字段已通过请求校验。").to_dict(),
                step("persist", "保存画像更正", "unknown", "写入过程异常，最终状态需要重新读取确认。").to_dict(),
                step("audit", "记录管理员操作", "skipped", "持久化结果未知。").to_dict(),
            ]
            report["partial"] = True
            report["outcome_unknown"] = True
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(
                    f"[persona operation] code=persona_correction_persist_failed "
                    f"exception={type(exc).__name__} trace={report.get('trace_id', '')}"
                )
            raise HTTPException(status_code=500, detail=report) from exc
        if entry is None:
            report = diagnostic(
                ok=False,
                code="persona_correction_unconfirmed",
                phase="persistence",
                title="画像更正结果无法确认",
                message="画像服务未返回可确认的持久化结果。",
                details=(operation_detail("目标用户", user_id), operation_detail("更正字段", list(corrections.keys()))),
                steps=(
                    step("validate", "校验画像更正", "ok", "更正字段已通过请求校验。"),
                    step("persist", "保存画像更正", "unknown", "未取得可确认的保存结果。"),
                    step("audit", "记录管理员操作", "skipped", "持久化结果未知。"),
                ),
                suggestion="刷新该用户画像确认当前字段；未生效时再提交更正。",
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
            raise HTTPException(status_code=500, detail=report)
        audit_ok = True
        try:
            webui_audit_log.record(
                action="persona_correction", qq=admin.qq, device_id=admin.device_id,
                target=str(user_id), detail={"fields": list(corrections.keys())}, outcome="ok",
            )
        except Exception as exc:
            audit_ok = False
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(f"[persona operation] audit_failed exception={type(exc).__name__}")
        report = diagnostic(
            ok=True,
            code="persona_correction_saved",
            phase="operation_complete",
            title="画像更正已保存",
            message="画像更正已持久化，并会在后续画像生成时保留。",
            details=(operation_detail("目标用户", user_id), operation_detail("更正字段", list(corrections.keys()), "ok")),
            steps=(
                step("validate", "校验画像更正", "ok", "更正字段有效。"),
                step("persist", "保存画像更正", "ok", "画像服务已确认保存。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "画像已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("画像更正已保存，但本次管理员审计记录未能写入。",),
            retryable=False,
            partial=not audit_ok,
            outcome_unknown=False,
        )
        return {"success": True, "profile_text": entry.data, **report}

    @router.post("/{user_id}/avatar-analysis/refresh")
    async def refresh_avatar_analysis(
        user_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        svc = _profile_service(runtime)
        bundle = _runtime_bundle(runtime)
        if svc is None or bundle is None:
            report = diagnostic(
                ok=False,
                code="avatar_analysis_unavailable",
                phase="precondition",
                title="头像分析暂不可用",
                message="画像运行时尚未就绪，未排队任何后台任务。",
                details=(operation_detail("目标用户", user_id),),
                steps=(
                    step("precondition", "检查画像服务", "error", "画像运行时未就绪。"),
                    step("schedule", "排队头像分析", "skipped", "未创建后台任务。"),
                    step("audit", "记录管理员操作", "skipped", "操作未进入执行阶段。"),
                ),
                suggestion="等待插件运行时完成初始化后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
            raise HTTPException(status_code=503, detail=report)
        snapshot = svc.get_core_profile(str(user_id))
        profile_json = snapshot.profile_json if snapshot is not None else {}
        qq_profile = profile_json.get("qq_profile", {}) if isinstance(profile_json, dict) else {}
        if not isinstance(qq_profile, dict) or not str(qq_profile.get("avatar_url", "") or "").strip():
            report = diagnostic(
                ok=False,
                code="avatar_analysis_profile_missing",
                phase="precondition",
                title="头像分析未排队",
                message="该用户尚无可供后台任务读取的 QQ 头像资料。",
                details=(operation_detail("目标用户", user_id),),
                steps=(
                    step("precondition", "检查 QQ 资料", "error", "未找到已持久化头像资料。"),
                    step("schedule", "排队头像分析", "skipped", "未创建后台任务。"),
                    step("audit", "记录管理员操作", "skipped", "操作未进入执行阶段。"),
                ),
                suggestion="让该用户先与 bot 产生一次消息交互，保存协议资料后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
            raise HTTPException(status_code=409, detail=report)
        try:
            task = force_user_avatar_analysis(bundle, str(user_id))
        except Exception as exc:
            report = exception_diagnostic(
                exc,
                phase="schedule",
                title="头像分析未排队",
                message="服务器未能创建头像分析后台任务。",
                suggestion="稍后重试；若持续失败，请根据 Trace ID 查看脱敏日志。",
                retryable=True,
            )
            report["code"] = "avatar_analysis_schedule_failed"
            report["details"] = [operation_detail("目标用户", user_id).to_dict(), *report.get("details", [])]
            report["steps"] = [
                step("precondition", "检查 QQ 资料", "ok", "已找到头像资料。").to_dict(),
                step("schedule", "排队头像分析", "error", "后台任务创建失败。").to_dict(),
                step("audit", "记录管理员操作", "skipped", "任务未排队。").to_dict(),
            ]
            raise HTTPException(status_code=500, detail=report) from exc
        if task is None:
            report = diagnostic(
                ok=False,
                code="avatar_analysis_schedule_unconfirmed",
                phase="schedule",
                title="头像分析未排队",
                message="画像服务没有返回可确认的后台任务。",
                details=(operation_detail("目标用户", user_id),),
                steps=(
                    step("precondition", "检查 QQ 资料", "ok", "已找到头像资料。"),
                    step("schedule", "排队头像分析", "error", "未取得后台任务。"),
                    step("audit", "记录管理员操作", "skipped", "任务未排队。"),
                ),
                suggestion="等待插件运行时稳定后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
            raise HTTPException(status_code=503, detail=report)
        audit_ok = _record_avatar_audit(
            runtime,
            action="avatar_analysis_refresh",
            qq=admin.qq,
            device_id=admin.device_id,
            target=str(user_id),
            detail={"force": True},
            outcome="ok",
        )
        report = diagnostic(
            ok=True,
            code="avatar_analysis_scheduled",
            phase="operation_complete",
            title="头像重新分析已排队",
            message="后台任务会安全下载当前头像并重新分析，不会阻塞聊天回复。",
            details=(operation_detail("目标用户", user_id), operation_detail("执行方式", "后台 force", "ok")),
            steps=(
                step("precondition", "检查 QQ 资料", "ok", "已找到头像资料。"),
                step("schedule", "排队头像分析", "ok", "后台任务已创建或已由同用户任务承接。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "管理员操作已记录。" if audit_ok else "任务已排队，但审计写入失败。"),
            ),
            warnings=() if audit_ok else ("头像分析已排队，但本次管理员审计记录未能写入。",),
            retryable=False,
            partial=not audit_ok,
            outcome_unknown=False,
        )
        return {"success": True, "scheduled": True, **report}

    @router.delete("/{user_id}/avatar-analysis")
    async def clear_avatar_analysis(
        user_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        svc = _profile_service(runtime)
        if svc is None:
            report = diagnostic(
                ok=False,
                code="avatar_analysis_unavailable",
                phase="precondition",
                title="头像分析删除暂不可用",
                message="画像运行时尚未就绪，未执行删除。",
                details=(operation_detail("目标用户", user_id),),
                steps=(
                    step("precondition", "检查画像服务", "error", "画像服务未就绪。"),
                    step("persist", "删除头像分析", "skipped", "未执行写入。"),
                    step("audit", "记录管理员操作", "skipped", "未执行删除。"),
                ),
                suggestion="等待插件运行时完成初始化后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
            raise HTTPException(status_code=503, detail=report)
        try:
            deleted = clear_user_avatar_analysis(svc, str(user_id))
        except Exception as exc:
            report = exception_diagnostic(
                exc,
                phase="persistence",
                title="头像分析删除未完成",
                message="服务器未能确认头像分析字段已删除。",
                suggestion="刷新画像详情确认当前状态；仍存在时再重试。",
                retryable=False,
            )
            report["code"] = "avatar_analysis_clear_failed"
            report["details"] = [operation_detail("目标用户", user_id).to_dict(), *report.get("details", [])]
            report["steps"] = [
                step("precondition", "检查画像服务", "ok", "画像服务已就绪。").to_dict(),
                step("persist", "删除头像分析", "unknown", "写入异常，最终状态需要重新读取。").to_dict(),
                step("audit", "记录管理员操作", "skipped", "删除结果未知。").to_dict(),
            ]
            report["partial"] = True
            report["outcome_unknown"] = True
            raise HTTPException(status_code=500, detail=report) from exc
        audit_ok = _record_avatar_audit(
            runtime,
            action="avatar_analysis_clear",
            qq=admin.qq,
            device_id=admin.device_id,
            target=str(user_id),
            detail={"deleted": bool(deleted)},
            outcome="ok",
        )
        report = diagnostic(
            ok=True,
            code="avatar_analysis_cleared" if deleted else "avatar_analysis_already_clear",
            phase="operation_complete",
            title="头像分析已删除" if deleted else "头像分析原本为空",
            message="已删除持久化的头像分析与安全摘要。" if deleted else "没有找到需要删除的头像分析字段。",
            details=(operation_detail("目标用户", user_id), operation_detail("已删除", bool(deleted), "ok")),
            steps=(
                step("precondition", "检查画像服务", "ok", "画像服务已就绪。"),
                step("persist", "删除头像分析", "ok", "持久化状态已确认。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "管理员操作已记录。" if audit_ok else "分析已删除，但审计写入失败。"),
            ),
            warnings=() if audit_ok else ("头像分析已删除，但本次管理员审计记录未能写入。",),
            retryable=False,
            partial=not audit_ok,
            outcome_unknown=False,
        )
        return {"success": True, "deleted": bool(deleted), **report}

    return router
