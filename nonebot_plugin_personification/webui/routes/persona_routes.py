from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core.onebot_cache import get_user_nickname, get_user_profile
from ...core.user_profile_meta import compact_user_profile_meta_summary
from ..deps import AdminIdentity, require_admin
from .favorability_view import serialize_favorability


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
            qq_profile = _merge_display_profile(stored_qq_profile, live_qq_profile)
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
    async def detail(user_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        svc = _profile_service(runtime)
        if svc is None:
            raise HTTPException(status_code=503, detail="profile_service 未就绪")
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
                }
            )
        core_json = (core.profile_json if core and isinstance(core.profile_json, dict) else {}) or {}
        stored_qq_profile = core_json.get("qq_profile", {}) if isinstance(core_json.get("qq_profile", {}), dict) else {}
        live_qq_profile = await get_user_profile(_get_first_bot(runtime), user_id)
        qq_profile = _merge_display_profile(stored_qq_profile, live_qq_profile)
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
                "profile_json": core_json,
                "structured": structured,
                "qq_profile": qq_profile,
                "user_corrections": core_json.get("user_corrections", {}),
                "updated_at": core.updated_at if core else 0,
            } if core else {
                "profile_text": "",
                "profile_json": {},
                "structured": {},
                "qq_profile": qq_profile,
                "user_corrections": {},
                "updated_at": 0,
            },
            "local_profiles": local_profiles,
            "prompt_block": svc.build_prompt_block(user_id=user_id, group_id=""),
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
            raise HTTPException(status_code=503, detail="persona_store 未就绪")
        corrections = body.get("corrections") or {}
        if not isinstance(corrections, dict) or not corrections:
            raise HTTPException(status_code=400, detail="corrections 不能为空")
        entry = await store.apply_user_correction(user_id, corrections)
        from ...core import webui_audit_log

        webui_audit_log.record(action="persona_correction", qq=admin.qq, device_id=admin.device_id,
                               target=str(user_id), detail={"fields": list(corrections.keys())}, outcome="ok")
        return {"success": True, "profile_text": entry.data if entry else ""}

    return router
