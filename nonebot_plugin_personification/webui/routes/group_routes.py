from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...core import webui_audit_log
from ...core.db import connect_sync
from ...core.group_member_aliases import (
    delete_group_member_aliases,
    list_group_member_aliases,
    merge_known_names,
    set_group_member_aliases,
)
from ...core.meme_dictionary import delete_meme_entry, list_meme_entries, upsert_meme_entry
from ...core.onebot_cache import get_group_name_map, get_user_nickname
from ..deps import AdminIdentity, get_client_ip, require_admin
from .favorability_view import serialize_favorability


def _profile_service(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "profile_service", None)


def _memory_store(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "memory_store", None)


async def _call_group_schedule_model(runtime: Any, group_id: str, messages: list[dict[str, str]], *, purpose: str) -> str:
    bundle = getattr(runtime, "runtime_bundle", None)
    caller = getattr(bundle, "call_ai_api", None)
    if not callable(caller):
        raise RuntimeError("主模型调用器未就绪")
    from ...core.llm_context import reset_llm_context, set_llm_context

    token = set_llm_context(purpose=purpose, group_id=str(group_id or ""))
    try:
        result = await caller(messages, use_builtin_search=False, temperature=0.15, timeout=60)
    finally:
        reset_llm_context(token)
    return str(result or "").strip()


def _group_schedule_context(runtime: Any, group_id: str) -> str:
    store = _memory_store(runtime)
    parts: list[str] = []
    if store is not None:
        try:
            profiles = list(store.list_local_profiles(str(group_id)))[:20]
            if profiles:
                lines = []
                for row in profiles[:12]:
                    uid = str(row.get("user_id", "") or "")
                    snippet = str(row.get("profile_text", "") or "").strip()[:160]
                    if uid and snippet:
                        lines.append(f"- {uid}: {snippet}")
                if lines:
                    parts.append("群成员画像：\n" + "\n".join(lines))
        except Exception:
            pass
        try:
            rows = list(store.list_recent_memories(group_id=str(group_id), limit=30))
            mems = [
                f"- {str(row.get('memory_type',''))}: {str(row.get('summary','')).strip()[:140]}"
                for row in rows[:12]
                if str(row.get("summary", "") or "").strip()
            ]
            if mems:
                parts.append("近期群记忆：\n" + "\n".join(mems))
        except Exception:
            pass
    if not parts:
        parts.append("（暂无足够群画像/记忆；请生成保守、可编辑的空白作息建议。）")
    return "\n\n".join(parts)


def _knowledge_autobuild_status(runtime, group_id: str) -> dict[str, Any]:
    """读取 group_knowledge_autobuild 的运行状态：上次时间、今日次数、距下次间隔。"""
    try:
        from ...core.data_store import get_data_store
        from ...core.group_knowledge_autobuild import (
            _DEFAULT_DAILY_LIMIT,
            _DEFAULT_INTERVAL_HOURS,
            _DEFAULT_MIN_MESSAGES,
            _NS_DAILY_COUNT,
            _NS_LAST_RUN,
            _today_key,
        )
    except Exception:
        return {"enabled": False, "error": "autobuild_module_missing"}
    enabled = bool(getattr(runtime.plugin_config, "personification_group_knowledge_autobuild_enabled", True))
    interval_hours = int(
        getattr(runtime.plugin_config, "personification_group_knowledge_interval_hours", _DEFAULT_INTERVAL_HOURS)
        or _DEFAULT_INTERVAL_HOURS
    )
    daily_limit = int(
        getattr(runtime.plugin_config, "personification_group_knowledge_daily_limit", _DEFAULT_DAILY_LIMIT)
        or _DEFAULT_DAILY_LIMIT
    )
    min_messages = int(
        getattr(runtime.plugin_config, "personification_group_knowledge_min_messages", _DEFAULT_MIN_MESSAGES)
        or _DEFAULT_MIN_MESSAGES
    )
    ds = get_data_store()
    last_run = 0.0
    daily_count = 0
    try:
        data = ds.load_sync(_NS_LAST_RUN)
        if isinstance(data, dict):
            last_run = float(data.get(str(group_id), 0) or 0)
        count_data = ds.load_sync(_NS_DAILY_COUNT)
        if isinstance(count_data, dict):
            today_bucket = count_data.get(_today_key(), {})
            if isinstance(today_bucket, dict):
                daily_count = int(today_bucket.get(str(group_id), 0) or 0)
    except Exception:
        pass
    return {
        "enabled": enabled,
        "interval_hours": interval_hours,
        "min_messages_threshold": min_messages,
        "daily_limit": daily_limit,
        "last_run_at": last_run,
        "daily_count": daily_count,
        "daily_limit_hit": daily_count >= daily_limit,
    }


def _get_first_bot(runtime) -> Any | None:
    for holder in (getattr(runtime, "runtime_bundle", None), runtime):
        if holder is None:
            continue
        get_bots = getattr(holder, "get_bots", None)
        if not callable(get_bots):
            continue
        try:
            bots = get_bots() or {}
        except Exception:
            continue
        bot = next(iter(bots.values()), None) if bots else None
        if bot is not None:
            return bot
    return None


def _load_recent_group_member_names(group_id: str, *, limit: int = 500) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    try:
        with connect_sync() as conn:
            rows = conn.execute(
                """
                SELECT user_id, nickname
                FROM group_messages
                WHERE group_id=? AND user_id<>'' AND nickname<>''
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (str(group_id), max(1, int(limit))),
            ).fetchall()
    except Exception:
        return {}
    for row in rows:
        uid = str(row["user_id"] if hasattr(row, "__getitem__") else "").strip()
        nickname = str(row["nickname"] if hasattr(row, "__getitem__") else "").strip()
        if not uid or not nickname:
            continue
        names[uid] = merge_known_names(names.get(uid, []), nickname)
    return names


def _load_member_relationship_edges(
    group_id: str,
    member_ids: set[str],
    *,
    names_by_user: dict[str, list[str]],
    limit_per_member: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    if not member_ids:
        return {}
    try:
        import time as _time

        from ...core.group_relation_edges import _decayed_weight

        with connect_sync() as conn:
            rows = conn.execute(
                """
                SELECT src_user_id, dst_user_id, edge_kind, weight, last_seen_at
                FROM group_relation_edges
                WHERE group_id=?
                ORDER BY last_seen_at DESC
                LIMIT 500
                """,
                (str(group_id),),
            ).fetchall()
        now_ts = _time.time()
    except Exception:
        return {}

    def _label(uid: str) -> str:
        names = merge_known_names(names_by_user.get(uid, []))
        return names[0] if names else uid

    by_user: dict[str, list[dict[str, Any]]] = {uid: [] for uid in member_ids}
    for row in rows:
        src = str(row["src_user_id"] if hasattr(row, "__getitem__") else "").strip()
        dst = str(row["dst_user_id"] if hasattr(row, "__getitem__") else "").strip()
        if not src or not dst or (src not in member_ids and dst not in member_ids):
            continue
        try:
            decayed = _decayed_weight(
                float(row["weight"] if hasattr(row, "__getitem__") else 0.0),
                float(row["last_seen_at"] if hasattr(row, "__getitem__") else 0.0),
                now_ts=now_ts,
            )
        except Exception:
            decayed = 0.0
        if decayed <= 0.1:
            continue
        edge_kind = str(row["edge_kind"] if hasattr(row, "__getitem__") else "").strip()
        last_seen_at = float(row["last_seen_at"] if hasattr(row, "__getitem__") else 0.0)
        if src in member_ids:
            by_user.setdefault(src, []).append(
                {
                    "direction": "out",
                    "peer_user_id": dst,
                    "peer_label": _label(dst),
                    "kind": edge_kind,
                    "weight": round(decayed, 2),
                    "last_seen_at": last_seen_at,
                }
            )
        if dst in member_ids:
            by_user.setdefault(dst, []).append(
                {
                    "direction": "in",
                    "peer_user_id": src,
                    "peer_label": _label(src),
                    "kind": edge_kind,
                    "weight": round(decayed, 2),
                    "last_seen_at": last_seen_at,
                }
            )
    for uid, edges in list(by_user.items()):
        edges.sort(key=lambda item: (-float(item.get("weight", 0) or 0), str(item.get("peer_user_id", ""))))
        by_user[uid] = edges[: max(1, int(limit_per_member))]
    return by_user


_REBUILD_RATELIMIT_NS = "group_style_rebuild_ratelimit"
_KNOWLEDGE_REBUILD_RATELIMIT_NS = "group_knowledge_rebuild_ratelimit"
_REBUILD_WINDOW_SECONDS = 300
_REBUILD_MAX_PER_WINDOW = 3


def _collect_all_known_groups(runtime) -> tuple[list[str], dict[str, str]]:
    """合并 memory_store / 动态白名单 / 配置白名单 / group_configs，返回去重排序的群号列表。

    第二个返回值是 group_id -> 来源标签（memory|dynamic|config|group_config）。"""
    from ...utils import load_whitelist, load_group_configs

    svc = _profile_service(runtime)
    known_from_svc = [str(g) for g in (svc.list_groups() if svc else [])]
    config_whitelist = [str(g) for g in (getattr(runtime.plugin_config, "personification_whitelist", []) or [])]
    dynamic_whitelist = [str(g) for g in load_whitelist()]
    group_configs = load_group_configs() if callable(load_group_configs) else {}
    if not isinstance(group_configs, dict):
        group_configs = {}
    config_keys = [str(g) for g in group_configs.keys()]
    all_ids = sorted(set(known_from_svc) | set(dynamic_whitelist) | set(config_whitelist) | set(config_keys))
    source: dict[str, str] = {}
    for gid in all_ids:
        if gid in known_from_svc:
            source[gid] = "memory"
        elif gid in config_keys:
            source[gid] = "group_config"
        elif gid in config_whitelist:
            source[gid] = "config_file"
        elif gid in dynamic_whitelist:
            source[gid] = "dynamic"
        else:
            source[gid] = "unknown"
    return all_ids, source


def build_group_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/groups", tags=["groups"])

    @router.get("")
    async def list_groups(_: AdminIdentity = Depends(require_admin)) -> dict:
        svc = _profile_service(runtime)
        all_ids, source_map = _collect_all_known_groups(runtime)
        bot = _get_first_bot(runtime)
        name_map = await get_group_name_map(bot, all_ids)
        items: list[dict[str, Any]] = []
        for gid in all_ids:
            items.append(
                {
                    "group_id": gid,
                    "group_name": name_map.get(gid, ""),
                    "source": source_map.get(gid, ""),
                    "has_memory": source_map.get(gid) == "memory",
                    "favorability": serialize_favorability(
                        runtime,
                        f"group_{gid}",
                        scope="group",
                        include_events=False,
                    ),
                }
            )
        return {"groups": items, "available": svc is not None}

    @router.get("/whitelist")
    async def get_group_switches(_: AdminIdentity = Depends(require_admin)) -> dict:
        from ...utils import load_whitelist, load_group_configs, is_group_whitelisted

        svc = _profile_service(runtime)
        bot = _get_first_bot(runtime)
        config_whitelist = list(getattr(runtime.plugin_config, "personification_whitelist", []) or [])
        dynamic_whitelist = load_whitelist()
        group_configs = load_group_configs()
        known_from_svc = [str(g) for g in (svc.list_groups() if svc else [])]
        all_ids = sorted({str(g) for g in known_from_svc} | set(dynamic_whitelist) | set(config_whitelist))
        name_map = await get_group_name_map(bot, all_ids)
        items: list[dict[str, Any]] = []
        for gid in all_ids:
            enabled = is_group_whitelisted(gid, config_whitelist)
            cfg = group_configs.get(gid, {}) if isinstance(group_configs, dict) else {}
            if not isinstance(cfg, dict):
                cfg = {}
            if "enabled" in cfg:
                source = "group_config"
            elif gid in config_whitelist:
                source = "config_file"
            elif gid in dynamic_whitelist:
                source = "dynamic"
            else:
                source = "none"
            items.append(
                {
                    "group_id": gid,
                    "group_name": name_map.get(gid, ""),
                    "enabled": enabled,
                    "source": source,
                    "readonly": gid in config_whitelist and "enabled" not in cfg,
                }
            )
        return {"groups": items}

    @router.post("/{group_id}/whitelist")
    async def enable_group(
        group_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...utils import add_group_to_whitelist

        added = add_group_to_whitelist(group_id)
        webui_audit_log.record(
            action="group_whitelist_add",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
        )
        return {"success": True, "added": added, "group_id": group_id}

    @router.delete("/{group_id}/whitelist")
    async def disable_group(
        group_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...utils import remove_group_from_whitelist

        removed = remove_group_from_whitelist(group_id)
        webui_audit_log.record(
            action="group_whitelist_remove",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
        )
        return {"success": True, "removed": removed, "group_id": group_id}

    @router.get("/{group_id}/personas")
    async def personas(group_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        svc = _profile_service(runtime)
        if svc is None:
            raise HTTPException(status_code=503, detail="profile_service 未就绪")
        profiles = svc.list_local_profiles(group_id)
        seen = {str(p.get("user_id", "")) for p in profiles}
        aliases_by_user = list_group_member_aliases(group_id)
        recent_names_by_user = _load_recent_group_member_names(group_id)
        # 本地群画像通常为空（没有专门的本地画像构建流程）。回退：把本群近期活跃成员
        # 的【全局画像】并进来，避免「群内成员画像」一直显示为 0。
        try:
            from ...utils import get_recent_group_msgs

            recent = get_recent_group_msgs(str(group_id), limit=200, expire_hours=0) or []
            active_uids: list[str] = []
            for m in recent:
                uid = str((m or {}).get("user_id", "") or "").strip()
                if uid and uid not in seen and uid not in active_uids:
                    active_uids.append(uid)
            for uid in active_uids:
                snap = svc.get_core_profile(uid)
                text = getattr(snap, "profile_text", "") if snap is not None else ""
                if not str(text or "").strip():
                    continue
                profiles.append({
                    "user_id": uid,
                    "profile_text": str(text),
                    "profile_json": {"scope": "global"},
                    "updated_at": float(getattr(snap, "updated_at", 0) or 0),
                })
                seen.add(uid)
        except Exception as exc:
            getattr(runtime, "logger", None) and runtime.logger.debug(f"[group personas] 全局画像回退失败: {exc}")
        for uid in sorted(set(aliases_by_user.keys()) - seen):
            profiles.append(
                {
                    "user_id": uid,
                    "profile_text": "",
                    "profile_json": {"scope": "group_alias"},
                    "updated_at": float(aliases_by_user.get(uid, {}).get("updated_at", 0) or 0),
                }
            )
            seen.add(uid)
        profiles.sort(key=lambda p: float(p.get("updated_at", 0) or 0), reverse=True)
        bot = _get_first_bot(runtime)

        names_for_edges = {
            uid: merge_known_names(names, aliases_by_user.get(uid, {}).get("aliases", []))
            for uid, names in recent_names_by_user.items()
        }
        for uid, entry in aliases_by_user.items():
            names_for_edges[uid] = merge_known_names(names_for_edges.get(uid, []), entry.get("aliases", []))
        relationship_edges = _load_member_relationship_edges(
            group_id,
            {str(p.get("user_id", "") or "").strip() for p in profiles if str(p.get("user_id", "") or "").strip()},
            names_by_user=names_for_edges,
        )

        # 拉一次 emotion_state 给每条 persona 附上"近期情绪"
        emotion_per_user: dict[str, dict[str, Any]] = {}
        try:
            from ...core.emotion_state import load_emotion_state

            emotion_state = await load_emotion_state()
            raw = (emotion_state.get("per_user", {}) or {})
            if isinstance(raw, dict):
                emotion_per_user = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            emotion_per_user = {}

        items: list[dict[str, Any]] = []
        for p in profiles:
            uid = str(p["user_id"])
            text = p.get("profile_text") or ""
            entry_emotion = emotion_per_user.get(str(uid), {})
            nickname = await get_user_nickname(bot, uid)
            alias_entry = aliases_by_user.get(
                str(uid),
                {"user_id": str(uid), "aliases": [], "note": "", "updated_at": 0.0, "updated_by": ""},
            )
            known_names = merge_known_names(
                nickname,
                recent_names_by_user.get(str(uid), []),
                alias_entry.get("aliases", []),
            )
            items.append(
                {
                    "user_id": uid,
                    "nickname": nickname,
                    "known_names": known_names,
                    "aliases": list(alias_entry.get("aliases") or []),
                    "alias_note": str(alias_entry.get("note", "") or ""),
                    "alias_updated_at": float(alias_entry.get("updated_at", 0) or 0),
                    "alias_updated_by": str(alias_entry.get("updated_by", "") or ""),
                    "snippet": str(text)[:240],
                    "profile_text": str(text),
                    "updated_at": p.get("updated_at", 0),
                    "latest_emotion": {
                        "user_attitude": str(entry_emotion.get("user_attitude", "") or "")[:60],
                        "bot_emotion": str(entry_emotion.get("bot_emotion", "") or "")[:60],
                        "expression_style": str(entry_emotion.get("expression_style", "") or "")[:60],
                        "updated_at": str(entry_emotion.get("updated_at", "") or ""),
                    },
                    "favorability": serialize_favorability(
                        runtime,
                        str(uid),
                        scope="user",
                        include_events=False,
                    ),
                    "relationship_edges": relationship_edges.get(str(uid), []),
                }
            )
        return {
            "group_id": group_id,
            "profiles": items,
            "group_aliases": sorted(aliases_by_user.values(), key=lambda item: str(item.get("user_id", ""))),
            "group_favorability": serialize_favorability(
                runtime,
                f"group_{group_id}",
                scope="group",
                include_events=True,
            ),
        }

    @router.get("/{group_id}/aliases")
    async def aliases(group_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        entries = list_group_member_aliases(group_id)
        return {
            "group_id": str(group_id),
            "aliases": sorted(entries.values(), key=lambda item: str(item.get("user_id", ""))),
        }

    @router.put("/{group_id}/aliases/{user_id}")
    async def save_aliases(
        group_id: str,
        user_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            payload = dict(body or {})
            aliases_value = payload.get("aliases", payload.get("alias_text", ""))
            entry = set_group_member_aliases(
                group_id,
                user_id,
                aliases_value,
                note=str(payload.get("note", "") or ""),
                updated_by=admin.qq,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        webui_audit_log.record(
            action="group_alias_upsert",
            qq=admin.qq,
            device_id=admin.device_id,
            target=f"{group_id}:{user_id}",
            ip_hash=get_client_ip(request),
            detail={"aliases": entry.get("aliases", []), "has_note": bool(entry.get("note"))},
        )
        entries = list_group_member_aliases(group_id)
        return {
            "success": True,
            "entry": entry,
            "aliases": sorted(entries.values(), key=lambda item: str(item.get("user_id", ""))),
        }

    @router.delete("/{group_id}/aliases/{user_id}")
    async def delete_aliases(
        group_id: str,
        user_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        changed = delete_group_member_aliases(group_id, user_id)
        webui_audit_log.record(
            action="group_alias_delete",
            qq=admin.qq,
            device_id=admin.device_id,
            target=f"{group_id}:{user_id}",
            ip_hash=get_client_ip(request),
            detail={"changed": changed},
        )
        entries = list_group_member_aliases(group_id)
        return {
            "success": True,
            "deleted": changed,
            "aliases": sorted(entries.values(), key=lambda item: str(item.get("user_id", ""))),
        }

    @router.get("/{group_id}/schedule")
    async def group_schedule(group_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        from ...utils import get_group_config

        cfg = get_group_config(str(group_id))
        return {
            "group_id": str(group_id),
            "enabled": bool(cfg.get("schedule_enabled", False)),
            "schedule_prompt": str(cfg.get("schedule_prompt", "") or ""),
            "global_enabled": bool(getattr(runtime.plugin_config, "personification_schedule_global", False)),
        }

    @router.put("/{group_id}/schedule")
    async def save_group_schedule(
        group_id: str,
        request: Request,
        payload: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...utils import set_group_schedule_enabled, set_group_schedule_prompt

        enabled = bool(payload.get("enabled", False))
        prompt = str(payload.get("schedule_prompt", payload.get("prompt", "")) or "").strip()
        set_group_schedule_enabled(str(group_id), enabled)
        set_group_schedule_prompt(str(group_id), prompt)
        webui_audit_log.record(
            action="group_schedule_update",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"enabled": enabled, "chars": len(prompt)},
        )
        return {"success": True, "group_id": str(group_id), "enabled": enabled, "schedule_prompt": prompt}

    @router.post("/{group_id}/schedule/auto-generate")
    async def auto_generate_group_schedule(
        group_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        context = _group_schedule_context(runtime, str(group_id))
        persona_hint = str(body.get("persona_hint", "") or "").strip()
        base_user = (
            "请为拟人 bot 生成一份可编辑的本群角色作息表。"
            "目标不是限制回复，而是给角色更自然的生活状态。"
            "只基于给定画像/群记忆/人设补充；证据不足要保守。"
            "\n\n群上下文：\n"
            f"{context}\n\n"
            f"人设补充：{persona_hint or '（无）'}"
        )
        focus_items = [
            ("日常节律子agent", "提取可推断的昼夜节律、在线时段、休息时段，不足就写未知"),
            ("群聊状态子agent", "判断哪些状态适合群聊中自然带出，哪些不应限制回复"),
            ("交叉验证子agent", "找出冲突、过度臆测和应留空的部分"),
        ]
        subagents: list[dict[str, str]] = []
        for name, focus in focus_items:
            raw = await _call_group_schedule_model(
                runtime,
                str(group_id),
                [
                    {"role": "system", "content": "你是作息表生成的只读子agent，只输出简短要点，不要写成最终表格。"},
                    {"role": "user", "content": f"{base_user}\n\n你的关注点：{focus}"},
                ],
                purpose="group_schedule_research",
            )
            subagents.append({"name": name, "focus": focus, "raw": raw[:1200]})
        synthesis = await _call_group_schedule_model(
            runtime,
            str(group_id),
            [
                {
                    "role": "system",
                    "content": (
                        "你是拟人插件的作息表合成器。输出一份可直接注入 prompt 的中文作息表，"
                        "默认保守、可编辑，不要编造学校/工作等身份。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{base_user}\n\n三个子agent报告：\n"
                        + json.dumps(subagents, ensure_ascii=False, indent=2)
                        + "\n\n输出要求：6-10 行以内；包含在线/休息/高能量/低能量/不确定项；"
                        "明确写“作息仅作背景，不限制是否回复”。只输出作息表正文。"
                    ),
                },
            ],
            purpose="group_schedule_synthesis",
        )
        prompt = synthesis.strip()[:1600]
        webui_audit_log.record(
            action="group_schedule_generate",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"chars": len(prompt), "subagent_count": len(subagents)},
        )
        return {"group_id": str(group_id), "schedule_prompt": prompt, "subagents": subagents}

    @router.get("/{group_id}/style")
    async def style(group_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        from ...core.group_style_autobuild import list_style_snapshots

        snapshots = list_style_snapshots(group_id, limit=3)
        latest = snapshots[0] if snapshots else None
        return {
            "group_id": group_id,
            "snapshots": snapshots,
            "style_text": latest["style_text"] if latest else "",
            "style_json": latest["style_json"] if latest else {},
            "updated_at": latest["created_at"] if latest else 0,
        }

    @router.post("/{group_id}/style/rebuild")
    async def style_rebuild(
        group_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        # 限流：同设备 5 分钟最多 3 次（避免误点滥用 token）
        from ...core.data_store import get_data_store

        rate_key = hashlib.sha256(f"{admin.device_id}:{group_id}".encode("utf-8")).hexdigest()[:24]
        store = get_data_store()
        now_ts = time.time()
        rl_data = store.load_sync(_REBUILD_RATELIMIT_NS) or {}
        if not isinstance(rl_data, dict):
            rl_data = {}
        bucket = rl_data.get(rate_key) if isinstance(rl_data.get(rate_key), dict) else {}
        if not isinstance(bucket, dict) or now_ts - float(bucket.get("window_start", 0) or 0) > _REBUILD_WINDOW_SECONDS:
            bucket = {"window_start": now_ts, "count": 0}
        if int(bucket.get("count", 0) or 0) >= _REBUILD_MAX_PER_WINDOW:
            raise HTTPException(status_code=429, detail=f"重建过于频繁，请 {int(_REBUILD_WINDOW_SECONDS / 60)} 分钟后重试")
        bucket["count"] = int(bucket.get("count", 0) or 0) + 1
        rl_data[rate_key] = bucket
        store.save_sync(_REBUILD_RATELIMIT_NS, rl_data)

        from ...core.group_style_autobuild import build_group_style, _load_messages_since, _format_chat_summary

        store_mem = _memory_store(runtime)
        if store_mem is None:
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
        bundle = getattr(runtime, "runtime_bundle", None)
        deps = getattr(bundle, "reply_processor_deps", None) if bundle else None
        runtime_inner = getattr(deps, "runtime", None) if deps else None
        tool_caller = getattr(runtime_inner, "agent_tool_caller", None) if runtime_inner else None
        if tool_caller is None:
            raise HTTPException(status_code=503, detail="tool_caller 未就绪")
        # 取最近 250 条对话强行喂给 LLM；跳过 daily_limit
        rows = _load_messages_since(memory_store=store_mem, group_id=group_id, since_ts=0, limit=250)
        if len(rows) < 20:
            raise HTTPException(status_code=400, detail=f"该群消息不足 20 条（当前 {len(rows)}），样本太少不构建")
        chat_summary = _format_chat_summary(rows)
        built = await build_group_style(
            tool_caller=tool_caller,
            memory_store=store_mem,
            group_id=group_id,
            chat_summary=chat_summary,
        )
        if not built:
            webui_audit_log.record(
                action="style_rebuild",
                qq=admin.qq,
                device_id=admin.device_id,
                target=group_id,
                outcome="llm_failed",
            )
            raise HTTPException(status_code=500, detail="LLM 返回的风格 JSON 解析失败")
        from ...core.group_style_autobuild import list_style_snapshots

        webui_audit_log.record(
            action="style_rebuild",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            detail={"snapshot_id": built.get("id")},
        )
        return {
            "success": True,
            "new_snapshot": built,
            "snapshots": list_style_snapshots(group_id, limit=3),
        }

    @router.get("/{group_id}/agent-state")
    async def agent_state(
        group_id: str,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """汇总群信息卡片需要的 agent 状态：情绪、最近记忆、关系 top 边、近期活跃。"""
        from ...core.emotion_state import (
            describe_group_emotion_memory,
            load_emotion_state,
            render_inner_state_hint,
        )

        store = _memory_store(runtime)
        emotion_state = await load_emotion_state()
        group_emotion = (emotion_state.get("per_group", {}) or {}).get(str(group_id), {}) or {}
        emotion_summary = describe_group_emotion_memory(emotion_state, str(group_id))

        # inner_state（全局 bot 内心基线）
        inner_state_hint = ""
        try:
            from ...agent.inner_state import get_personification_data_dir, load_inner_state

            data_dir = get_personification_data_dir(runtime.plugin_config)
            loaded = await load_inner_state(data_dir)
            if isinstance(loaded, dict):
                inner_state_hint = render_inner_state_hint(loaded)
        except Exception:
            inner_state_hint = ""

        # 最近记忆 top-10 by salience
        recent_memories: list[dict[str, Any]] = []
        if store is not None:
            try:
                rows = list(store.list_recent_memories(group_id=str(group_id), limit=50))
                rows.sort(key=lambda m: float(m.get("salience", 0) or 0), reverse=True)
                for row in rows[:10]:
                    recent_memories.append(
                        {
                            "memory_id": row.get("memory_id", ""),
                            "memory_type": row.get("memory_type", ""),
                            "summary": str(row.get("summary", "") or "")[:160],
                            "salience": float(row.get("salience", 0) or 0),
                            "confidence": float(row.get("confidence", 0) or 0),
                            "updated_at": float(row.get("updated_at", 0) or 0),
                        }
                    )
            except Exception:
                recent_memories = []

        # 关系图 top 边
        top_edges: list[dict[str, Any]] = []
        try:
            import time as _time

            with connect_sync() as conn:
                rows = conn.execute(
                    """
                    SELECT src_user_id, dst_user_id, edge_kind, weight, last_seen_at
                    FROM group_relation_edges
                    WHERE group_id=?
                    ORDER BY last_seen_at DESC
                    LIMIT 80
                    """,
                    (str(group_id),),
                ).fetchall()
            now_ts = _time.time()
            from ...core.group_relation_edges import _decayed_weight

            aliases_by_user = list_group_member_aliases(group_id)
            recent_names_by_user = _load_recent_group_member_names(group_id)

            def _member_label(uid: str) -> str:
                alias_entry = aliases_by_user.get(uid, {})
                names = merge_known_names(recent_names_by_user.get(uid, []), alias_entry.get("aliases", []))
                return names[0] if names else uid

            scored = []
            for row in rows:
                w = _decayed_weight(float(row["weight"] or 0), float(row["last_seen_at"] or 0), now_ts=now_ts)
                if w <= 0.1:
                    continue
                src = str(row["src_user_id"] or "")
                dst = str(row["dst_user_id"] or "")
                scored.append(
                    {
                        "src": src,
                        "dst": dst,
                        "src_label": _member_label(src),
                        "dst_label": _member_label(dst),
                        "src_aliases": list((aliases_by_user.get(src) or {}).get("aliases") or []),
                        "dst_aliases": list((aliases_by_user.get(dst) or {}).get("aliases") or []),
                        "kind": str(row["edge_kind"] or ""),
                        "weight": round(w, 2),
                        "last_seen_at": float(row["last_seen_at"] or 0),
                    }
                )
            scored.sort(key=lambda e: e["weight"], reverse=True)
            top_edges = scored[:24]
        except Exception:
            top_edges = []

        # 群消息计数 + 最近一次活跃
        message_count = 0
        last_activity_ts = 0.0
        if store is not None:
            try:
                from ...core.memory_store import _connect

                group_dir = store.ensure_group_space(str(group_id))
                with _connect(group_dir / "chat_history.db") as conn:
                    row = conn.execute("SELECT COUNT(1) AS cnt, MAX(created_at) AS last_ts FROM messages").fetchone()
                    if row is not None:
                        message_count = int(row["cnt"] or 0)
                        last_activity_ts = float(row["last_ts"] or 0)
            except Exception:
                pass

        return {
            "group_id": str(group_id),
            "emotion": {
                "summary": emotion_summary,
                "raw": group_emotion,
                "global_inner_state": inner_state_hint,
            },
            "recent_memories": recent_memories,
            "top_edges": top_edges,
            "stats": {
                "message_count": message_count,
                "last_activity_at": last_activity_ts,
            },
        }

    @router.get("/{group_id}/memory/recent")
    async def memory_recent(
        group_id: str,
        limit: int = 30,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _memory_store(runtime)
        if store is None:
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
        try:
            items = list(store.list_recent_memories(group_id=group_id, limit=max(1, min(int(limit), 100))))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"group_id": group_id, "items": items}

    @router.get("/{group_id}/knowledge")
    async def group_knowledge(
        group_id: str,
        limit: int = 1000,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _memory_store(runtime)
        if store is None:
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
        per_type_limit = max(1, min(int(limit), 2000))
        knowledge: list[dict[str, Any]] = []
        try:
            for memory_type in ("group_knowledge", "group_meme", "concept_anchor"):
                items = list(
                    store.list_recent_memories(
                        group_id=group_id,
                        limit=per_type_limit,
                        memory_type=memory_type,
                    )
                )
                for item in items:
                    summary = str(item.get("summary", "") or "")
                    term = str(item.get("term", "") or "") or (summary.split(":", 1)[0] if ":" in summary else summary)
                    definition = str(item.get("definition", "") or "")
                    if not definition and ":" in summary:
                        definition = summary.split(":", 1)[1].strip()
                    if not definition:
                        definition = summary
                    knowledge.append(
                        {
                            "term": term,
                            "definition": definition,
                            "memory_type": memory_type,
                            "source_kind": item.get("source_kind", ""),
                            "confidence": float(item.get("confidence", 0) or 0),
                            "updated_at": float(item.get("updated_at", 0) or 0),
                        }
                    )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        # 去重：同 term 保留最新一条
        seen: dict[str, dict[str, Any]] = {}
        for entry in knowledge:
            key = entry["term"]
            if key not in seen or entry["updated_at"] > seen[key]["updated_at"]:
                seen[key] = entry
        deduped = sorted(seen.values(), key=lambda x: x.get("updated_at", 0), reverse=True)
        # 附带自动构建状态信息
        autobuild_status = _knowledge_autobuild_status(runtime, group_id)
        return {
            "group_id": group_id,
            "knowledge": deduped[:per_type_limit],
            "autobuild_status": autobuild_status,
        }

    @router.post("/{group_id}/knowledge/rebuild")
    async def knowledge_rebuild(
        group_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.data_store import get_data_store

        rate_key = hashlib.sha256(f"{admin.device_id}:{group_id}:knowledge".encode("utf-8")).hexdigest()[:24]
        ds = get_data_store()
        now_ts = time.time()
        rl_data = ds.load_sync(_KNOWLEDGE_REBUILD_RATELIMIT_NS) or {}
        if not isinstance(rl_data, dict):
            rl_data = {}
        bucket = rl_data.get(rate_key) if isinstance(rl_data.get(rate_key), dict) else {}
        if not isinstance(bucket, dict) or now_ts - float(bucket.get("window_start", 0) or 0) > _REBUILD_WINDOW_SECONDS:
            bucket = {"window_start": now_ts, "count": 0}
        if int(bucket.get("count", 0) or 0) >= _REBUILD_MAX_PER_WINDOW:
            raise HTTPException(status_code=429, detail=f"重建过于频繁，请 {int(_REBUILD_WINDOW_SECONDS / 60)} 分钟后重试")
        bucket["count"] = int(bucket.get("count", 0) or 0) + 1
        rl_data[rate_key] = bucket
        ds.save_sync(_KNOWLEDGE_REBUILD_RATELIMIT_NS, rl_data)

        from ...core.group_knowledge import build_group_knowledge
        from ...core.group_knowledge_autobuild import _load_messages_since, _format_chat_summary

        store_mem = _memory_store(runtime)
        if store_mem is None:
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
        bundle = getattr(runtime, "runtime_bundle", None)
        deps = getattr(bundle, "reply_processor_deps", None) if bundle else None
        runtime_inner = getattr(deps, "runtime", None) if deps else None
        tool_caller = getattr(runtime_inner, "agent_tool_caller", None) if runtime_inner else None
        if tool_caller is None:
            raise HTTPException(status_code=503, detail="tool_caller 未就绪")
        rows = _load_messages_since(memory_store=store_mem, group_id=group_id, since_ts=0, limit=200)
        if len(rows) < 20:
            raise HTTPException(status_code=400, detail=f"该群消息不足 20 条（当前 {len(rows)}），样本太少不构建")
        chat_summary = _format_chat_summary(rows)
        saved = await build_group_knowledge(
            tool_caller=tool_caller,
            memory_store=store_mem,
            group_id=group_id,
            chat_summary=chat_summary,
        )
        webui_audit_log.record(
            action="knowledge_rebuild",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"saved": int(saved)},
        )
        return {"success": True, "saved": int(saved)}

    @router.get("/{group_id}/memes")
    async def group_memes(
        group_id: str,
        limit: int = 1000,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            items = list_meme_entries(group_id=group_id, limit=max(1, min(int(limit), 2000)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"group_id": group_id, "memes": items}

    @router.post("/{group_id}/memes")
    async def save_group_meme(
        group_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        payload = dict(body or {})
        scope = str(payload.get("scope", "group") or "group").strip().lower()
        if scope not in {"group", "concept"}:
            scope = "group"
        payload["scope"] = scope
        payload["group_id"] = str(group_id)
        ok = upsert_meme_entry(payload)
        if not ok:
            raise HTTPException(status_code=400, detail="term 和 meaning/definition 不能为空")
        webui_audit_log.record(
            action="meme_upsert",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"term": payload.get("term"), "scope": scope},
        )
        return {"success": True, "entry": payload}

    @router.delete("/{group_id}/memes/{term}")
    async def delete_group_meme(
        group_id: str,
        term: str,
        request: Request,
        scope: str = "group",
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        normalized_scope = scope if scope in {"group", "concept"} else "group"
        changed = delete_meme_entry(term=term, scope=normalized_scope, group_id=group_id)
        webui_audit_log.record(
            action="meme_delete",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"term": term, "scope": normalized_scope, "changed": changed},
        )
        return {"success": True, "deleted": changed}

    return router
