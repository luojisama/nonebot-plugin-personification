from __future__ import annotations

import hashlib
import json
import re
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
from ...core.group_directory import discover_group_union
from ...core.onebot_cache import get_user_nickname
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ..deps import AdminIdentity, get_client_ip, require_admin
from .favorability_view import serialize_favorability


class _ModelResponseView:
    def __init__(self, response: Any, *, content: str) -> None:
        self._response = response
        self.content = content

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _operation_success(
    payload: dict[str, Any],
    *,
    code: str,
    phase: str,
    title: str,
    message: str,
    details: tuple = (),
    steps: tuple = (),
    warnings: tuple[str, ...] = (),
    suggestion: str = "",
    partial: bool = False,
) -> dict[str, Any]:
    report = diagnostic(
        ok=True,
        code=code,
        phase=phase,
        title=title,
        message=message,
        details=details,
        steps=steps,
        warnings=warnings,
        suggestion=suggestion,
        retryable=False,
        partial=partial,
        outcome_unknown=False,
    )
    return {**payload, **report}


def _raise_operation_failure(*, status_code: int, cause: BaseException | None = None, **kwargs: Any) -> None:
    report = diagnostic(ok=False, **kwargs)
    raise HTTPException(status_code=status_code, detail=report) from cause


def _raise_unexpected_failure(
    runtime: Any,
    exc: BaseException,
    *,
    status_code: int,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    steps: tuple,
    details: tuple = (),
    retryable: bool = False,
    partial: bool = False,
    outcome_unknown: bool = False,
) -> None:
    report = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message=message,
        suggestion=suggestion,
        retryable=retryable,
    )
    report["code"] = code
    report["steps"] = [item.to_dict() for item in steps]
    report["details"] = [item.to_dict() for item in details] + report.get("details", [])
    report["partial"] = bool(partial)
    report["outcome_unknown"] = bool(outcome_unknown)
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.warning(
            f"[group operation] code={code} phase={phase} exception={type(exc).__name__} "
            f"trace={report.get('trace_id', '')}"
        )
    raise HTTPException(status_code=status_code, detail=report) from exc


def _record_audit(runtime: Any, **kwargs: Any) -> bool:
    try:
        webui_audit_log.record(**kwargs)
        return True
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(
                f"[group operation] audit_failed action={kwargs.get('action', '')} "
                f"exception={type(exc).__name__}"
            )
        return False


def _parse_json_value(text: str, *, container: str) -> tuple[Any, str]:
    content = str(text or "").strip()
    if not content:
        return None, "empty"
    try:
        return json.loads(content), "ok"
    except json.JSONDecodeError:
        pattern = r"\{[\s\S]*\}" if container == "object" else r"\[[\s\S]*\]"
        match = re.search(pattern, content)
        if not match:
            return None, "json"
        try:
            return json.loads(match.group(0)), "ok"
        except json.JSONDecodeError:
            return None, "json"


def _valid_group_style_schema(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    text_fields = ("tone", "pace", "typical_length")
    list_fields = ("catchphrases", "taboos")
    if any(not isinstance(value.get(key), str) or not str(value.get(key) or "").strip() for key in text_fields):
        return False
    return all(
        isinstance(value.get(key), list)
        and all(isinstance(item, str) and item.strip() for item in value.get(key, []))
        for key in list_fields
    )


def _valid_group_knowledge_schema(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if len(str(item.get("term", "") or "").strip()) < 2:
            return False
        if not str(item.get("definition", "") or "").strip():
            return False
        if "aliases" in item and not isinstance(item.get("aliases"), list):
            return False
        if "tone" in item and not isinstance(item.get("tone"), list):
            return False
        if str(item.get("scope", "group") or "group").strip().lower() not in {"group", "concept"}:
            return False
        if str(item.get("risk_level", "low") or "low").strip().lower() not in {"low", "medium", "high"}:
            return False
    return True


class _ModelOutputProbe:
    def __init__(self, caller: Any, *, output_kind: str) -> None:
        self._caller = caller
        self.output_kind = output_kind
        self.failure_kind = ""

    async def chat_with_tools(self, *args: Any, **kwargs: Any) -> Any:
        try:
            response = await self._caller.chat_with_tools(*args, **kwargs)
        except Exception:
            self.failure_kind = "caller"
            raise
        content = str(getattr(response, "content", "") or "").strip()
        container = "object" if self.output_kind == "style" else "array"
        parsed, state = _parse_json_value(content, container=container)
        if state != "ok":
            self.failure_kind = state
            return response
        valid = _valid_group_style_schema(parsed) if self.output_kind == "style" else _valid_group_knowledge_schema(parsed)
        if not valid:
            self.failure_kind = "schema"
            return _ModelResponseView(response, content="")
        self.failure_kind = ""
        return response


class _KnowledgePersistenceProbe:
    def __init__(self, store: Any) -> None:
        self._store = store
        self.confirmed_writes = 0

    def write_memory_item(self, item: dict[str, Any]) -> Any:
        result = self._store.write_memory_item(item)
        self.confirmed_writes += 1
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


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


def build_group_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/groups", tags=["groups"])

    @router.get("")
    async def list_groups(_: AdminIdentity = Depends(require_admin)) -> dict:
        svc = _profile_service(runtime)
        groups = await discover_group_union(runtime)
        items: list[dict[str, Any]] = []
        for group in groups:
            gid = str(group["group_id"])
            sources = list(group.get("sources", []))
            items.append(
                {
                    **group,
                    "group_id": gid,
                    "source": sources[0] if len(sources) == 1 else "union",
                    "has_memory": "profile_memory" in sources,
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

        config_whitelist = list(getattr(runtime.plugin_config, "personification_whitelist", []) or [])
        dynamic_whitelist = load_whitelist()
        group_configs = load_group_configs()
        groups = await discover_group_union(runtime)
        items: list[dict[str, Any]] = []
        for group in groups:
            gid = str(group["group_id"])
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
                    "group_name": group.get("group_name", ""),
                    "enabled": enabled,
                    "source": source,
                    "readonly": False,
                    "static_config_readonly": gid in config_whitelist,
                    "static_config_note": "静态白名单只读；此开关写入 group_config.enabled 并优先覆盖" if gid in config_whitelist else "",
                    "sources": group.get("sources", []),
                }
            )
        return {"groups": items}

    @router.post("/{group_id}/whitelist")
    async def enable_group(
        group_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...utils import set_group_enabled

        try:
            set_group_enabled(group_id, True)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_whitelist_persist_failed",
                phase="persistence",
                title="群功能启用状态保存失败",
                message="服务器未能确认群功能启用状态已保存。",
                suggestion="先刷新群开关状态；确认未生效后再重试。",
                details=(detail("目标群", group_id), detail("目标状态", "enabled")),
                steps=(
                    step("persist", "保存群功能开关", "unknown", "写入过程异常，最终状态需要重新读取确认。"),
                    step("audit", "记录管理员操作", "skipped", "状态写入未得到明确结果。"),
                ),
                partial=True,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="group_whitelist_add",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
        )
        return _operation_success(
            {"success": True, "enabled": True, "group_id": group_id, "authority": "group_config.enabled"},
            code="group_whitelist_enabled",
            phase="operation_complete",
            title="群功能已启用",
            message="权威群配置已保存为 enabled。",
            details=(detail("目标群", group_id), detail("配置来源", "group_config.enabled", "ok")),
            steps=(
                step("persist", "保存群功能开关", "ok", "启用状态已写入权威群配置。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "状态已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("群功能已启用，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

    @router.delete("/{group_id}/whitelist")
    async def disable_group(
        group_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...utils import set_group_enabled

        try:
            set_group_enabled(group_id, False)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_whitelist_persist_failed",
                phase="persistence",
                title="群功能停用状态保存失败",
                message="服务器未能确认群功能停用状态已保存。",
                suggestion="先刷新群开关状态；确认未生效后再重试。",
                details=(detail("目标群", group_id), detail("目标状态", "disabled")),
                steps=(
                    step("persist", "保存群功能开关", "unknown", "写入过程异常，最终状态需要重新读取确认。"),
                    step("audit", "记录管理员操作", "skipped", "状态写入未得到明确结果。"),
                ),
                partial=True,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="group_whitelist_remove",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
        )
        return _operation_success(
            {"success": True, "enabled": False, "group_id": group_id, "authority": "group_config.enabled"},
            code="group_whitelist_disabled",
            phase="operation_complete",
            title="群功能已停用",
            message="权威群配置已保存为 disabled。",
            details=(detail("目标群", group_id), detail("配置来源", "group_config.enabled", "ok")),
            steps=(
                step("persist", "保存群功能开关", "ok", "停用状态已写入权威群配置。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "状态已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("群功能已停用，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

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
            _raise_operation_failure(
                status_code=400,
                cause=exc,
                code="group_alias_invalid",
                phase="request_validation",
                title="群成员称呼未保存",
                message="提交的群成员称呼格式或数量不符合要求。",
                details=(detail("目标群", group_id), detail("目标成员", user_id)),
                steps=(
                    step("validate", "校验群成员称呼", "error", "称呼数据未通过校验。"),
                    step("persist", "保存群成员称呼", "skipped", "未写入任何称呼数据。"),
                ),
                suggestion="检查称呼内容、长度和数量后重新提交。",
                retryable=False,
                partial=False,
                outcome_unknown=False,
            )
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_alias_persist_failed",
                phase="persistence",
                title="群成员称呼保存失败",
                message="服务器未能确认群成员称呼已完整保存。",
                suggestion="先刷新该成员的群称呼；确认状态后再决定是否重试。",
                details=(detail("目标群", group_id), detail("目标成员", user_id)),
                steps=(
                    step("validate", "校验群成员称呼", "ok", "提交内容已进入保存阶段。"),
                    step("persist", "保存群成员称呼", "unknown", "写入过程异常，最终状态需要重新读取确认。"),
                ),
                partial=True,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="group_alias_upsert",
            qq=admin.qq,
            device_id=admin.device_id,
            target=f"{group_id}:{user_id}",
            ip_hash=get_client_ip(request),
            detail={"aliases": entry.get("aliases", []), "has_note": bool(entry.get("note"))},
        )
        try:
            entries = list_group_member_aliases(group_id)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_alias_refresh_failed",
                phase="verification",
                title="群成员称呼已保存，但列表刷新失败",
                message="目标成员称呼已明确保存，服务器未能重新读取完整称呼列表。",
                suggestion="刷新群详情即可重新读取；不要重复保存同一内容。",
                details=(detail("目标群", group_id), detail("目标成员", user_id), detail("保存状态", "confirmed", "ok")),
                steps=(
                    step("persist", "保存群成员称呼", "ok", "目标成员称呼已保存。"),
                    step("verify", "重新读取称呼列表", "error", "完整列表读取失败。"),
                ),
                partial=True,
                outcome_unknown=False,
            )
        return _operation_success(
            {
                "success": True,
                "entry": entry,
                "aliases": sorted(entries.values(), key=lambda item: str(item.get("user_id", ""))),
            },
            code="group_alias_saved",
            phase="operation_complete",
            title="群成员称呼已保存",
            message="称呼映射已持久化并重新读取确认。",
            details=(
                detail("目标群", group_id),
                detail("目标成员", user_id),
                detail("称呼数量", len(entry.get("aliases", []) or []), "ok"),
            ),
            steps=(
                step("validate", "校验群成员称呼", "ok", "称呼格式有效。"),
                step("persist", "保存群成员称呼", "ok", "称呼映射已写入。"),
                step("verify", "重新读取称呼列表", "ok", "已确认保存结果可读取。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "称呼已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("称呼映射已保存，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

    @router.delete("/{group_id}/aliases/{user_id}")
    async def delete_aliases(
        group_id: str,
        user_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            changed = delete_group_member_aliases(group_id, user_id)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_alias_delete_failed",
                phase="persistence",
                title="群成员称呼删除失败",
                message="服务器未能确认群成员称呼已删除。",
                suggestion="先刷新该成员的群称呼；确认仍存在后再重试。",
                details=(detail("目标群", group_id), detail("目标成员", user_id)),
                steps=(step("persist", "删除群成员称呼", "unknown", "删除过程异常，最终状态需要重新读取确认。"),),
                partial=True,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="group_alias_delete",
            qq=admin.qq,
            device_id=admin.device_id,
            target=f"{group_id}:{user_id}",
            ip_hash=get_client_ip(request),
            detail={"changed": changed},
        )
        try:
            entries = list_group_member_aliases(group_id)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_alias_refresh_failed",
                phase="verification",
                title="群成员称呼已删除，但列表刷新失败",
                message="删除结果已明确返回，服务器未能重新读取完整称呼列表。",
                suggestion="刷新群详情即可重新读取；不要重复删除。",
                details=(detail("目标群", group_id), detail("目标成员", user_id), detail("已删除", changed, "ok")),
                steps=(
                    step("persist", "删除群成员称呼", "ok", "删除操作已明确完成。"),
                    step("verify", "重新读取称呼列表", "error", "完整列表读取失败。"),
                ),
                partial=True,
                outcome_unknown=False,
            )
        return _operation_success(
            {
                "success": True,
                "deleted": changed,
                "aliases": sorted(entries.values(), key=lambda item: str(item.get("user_id", ""))),
            },
            code="group_alias_deleted" if changed else "group_alias_delete_noop",
            phase="operation_complete",
            title="群成员称呼已删除" if changed else "群成员称呼无需删除",
            message="称呼映射已删除并重新读取确认。" if changed else "该成员当前没有可删除的群称呼映射。",
            details=(detail("目标群", group_id), detail("目标成员", user_id), detail("删除结果", changed, "ok")),
            steps=(
                step("persist", "删除群成员称呼", "ok", "删除操作已明确完成。"),
                step("verify", "重新读取称呼列表", "ok", "已确认删除后的列表可读取。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "删除结果已确认，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("删除结果已确认，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

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
        enabled_saved = False
        try:
            set_group_schedule_enabled(str(group_id), enabled)
            enabled_saved = True
            set_group_schedule_prompt(str(group_id), prompt)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_schedule_persist_failed",
                phase="persistence",
                title="群作息表保存失败",
                message="群作息配置未能完整保存，当前最终状态需要重新读取确认。",
                suggestion="先刷新群作息配置；确认当前值后再提交完整目标配置。",
                details=(
                    detail("目标群", group_id),
                    detail("启用开关写入", "confirmed" if enabled_saved else "unknown", "ok" if enabled_saved else "warn"),
                    detail("作息正文长度", len(prompt)),
                ),
                steps=(
                    step("persist_enabled", "保存作息启用状态", "ok" if enabled_saved else "unknown", "启用状态已保存。" if enabled_saved else "启用状态未得到明确结果。"),
                    step("persist_prompt", "保存作息正文", "unknown" if enabled_saved else "skipped", "作息正文写入未得到明确结果。" if enabled_saved else "前一步异常，未进入正文写入。"),
                    step("audit", "记录管理员操作", "skipped", "作息配置未完整保存。"),
                ),
                retryable=False,
                partial=enabled_saved,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="group_schedule_update",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"enabled": enabled, "chars": len(prompt)},
        )
        return _operation_success(
            {"success": True, "group_id": str(group_id), "enabled": enabled, "schedule_prompt": prompt},
            code="group_schedule_saved",
            phase="operation_complete",
            title="群作息表已保存",
            message="作息启用状态和作息正文均已持久化。",
            details=(
                detail("目标群", group_id),
                detail("启用状态", enabled, "ok"),
                detail("作息正文长度", len(prompt), "ok"),
            ),
            steps=(
                step("persist_enabled", "保存作息启用状态", "ok", "启用状态已保存。"),
                step("persist_prompt", "保存作息正文", "ok", "作息正文已保存。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "作息配置已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("群作息配置已保存，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

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
        research_steps = []
        for index, (name, focus) in enumerate(focus_items, start=1):
            try:
                raw = await _call_group_schedule_model(
                    runtime,
                    str(group_id),
                    [
                        {"role": "system", "content": "你是作息表生成的只读子agent，只输出简短要点，不要写成最终表格。"},
                        {"role": "user", "content": f"{base_user}\n\n你的关注点：{focus}"},
                    ],
                    purpose="group_schedule_research",
                )
            except Exception as exc:
                _raise_unexpected_failure(
                    runtime,
                    exc,
                    status_code=502,
                    code="group_schedule_research_failed",
                    phase="research",
                    title="群作息表研究阶段失败",
                    message="只读研究子任务未能全部完成，尚未进入最终合成。",
                    suggestion="检查主模型可用性后重试生成；当前没有修改群作息配置。",
                    details=(
                        detail("目标群", group_id),
                        detail("已完成子任务", len(subagents)),
                        detail("子任务总数", len(focus_items)),
                    ),
                    steps=(
                        *research_steps,
                        step(f"research_{index}", name, "error", "模型调用异常，未取得可用研究结果。"),
                        step("synthesis", "合成群作息表", "skipped", "研究阶段未完整结束。"),
                    ),
                    retryable=True,
                )
            if not raw:
                _raise_operation_failure(
                    status_code=502,
                    code="group_schedule_research_empty",
                    phase="research",
                    title="群作息表研究结果为空",
                    message="只读研究子任务返回了空结果，尚未进入最终合成。",
                    details=(detail("目标群", group_id), detail("已完成子任务", len(subagents)), detail("子任务总数", len(focus_items))),
                    steps=(
                        *research_steps,
                        step(f"research_{index}", name, "error", "模型返回空内容。"),
                        step("synthesis", "合成群作息表", "skipped", "研究阶段没有完整结果。"),
                    ),
                    suggestion="检查主模型输出后重试生成；当前没有修改群作息配置。",
                    retryable=True,
                    partial=False,
                    outcome_unknown=False,
                )
            subagents.append({"name": name, "focus": focus, "raw": raw[:1200]})
            research_steps.append(step(f"research_{index}", name, "ok", "已取得只读研究结果。"))
        try:
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
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=502,
                code="group_schedule_synthesis_failed",
                phase="synthesis",
                title="群作息表合成失败",
                message="研究子任务已完成，但最终作息表合成没有取得结果。",
                suggestion="检查主模型可用性后重试生成；当前没有修改群作息配置。",
                details=(detail("目标群", group_id), detail("已完成子任务", len(subagents), "ok")),
                steps=(*research_steps, step("synthesis", "合成群作息表", "error", "模型调用异常，未取得最终正文。")),
                retryable=True,
            )
        prompt = synthesis.strip()[:1600]
        if not prompt:
            _raise_operation_failure(
                status_code=502,
                code="group_schedule_synthesis_empty",
                phase="synthesis",
                title="群作息表合成结果为空",
                message="研究子任务已完成，但最终合成返回了空正文。",
                details=(detail("目标群", group_id), detail("已完成子任务", len(subagents), "ok")),
                steps=(*research_steps, step("synthesis", "合成群作息表", "error", "模型返回空内容。")),
                suggestion="检查主模型输出后重试生成；当前没有修改群作息配置。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        audit_ok = _record_audit(
            runtime,
            action="group_schedule_generate",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"chars": len(prompt), "subagent_count": len(subagents)},
        )
        return _operation_success(
            {"group_id": str(group_id), "schedule_prompt": prompt, "subagents": subagents},
            code="group_schedule_generated",
            phase="operation_complete",
            title="群作息表草稿已生成",
            message="三个只读研究子任务和最终合成均已完成；草稿尚未自动保存。",
            details=(
                detail("目标群", group_id),
                detail("研究子任务", len(subagents), "ok"),
                detail("作息正文长度", len(prompt), "ok"),
                detail("持久化状态", "not_saved", "info"),
            ),
            steps=(
                *research_steps,
                step("synthesis", "合成群作息表", "ok", "已生成可编辑作息正文。"),
                step("persist", "保存群作息表", "skipped", "自动生成接口只返回草稿，由保存接口持久化。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "草稿已生成，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("作息草稿已生成，但本次管理员审计记录未能写入。",),
            suggestion="确认并编辑草稿后，再调用群作息保存接口。",
            partial=not audit_ok,
        )

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
        try:
            store = get_data_store()
            now_ts = time.time()
            rl_data = store.load_sync(_REBUILD_RATELIMIT_NS) or {}
            if not isinstance(rl_data, dict):
                rl_data = {}
            bucket = rl_data.get(rate_key) if isinstance(rl_data.get(rate_key), dict) else {}
            if not isinstance(bucket, dict) or now_ts - float(bucket.get("window_start", 0) or 0) > _REBUILD_WINDOW_SECONDS:
                bucket = {"window_start": now_ts, "count": 0}
            if int(bucket.get("count", 0) or 0) >= _REBUILD_MAX_PER_WINDOW:
                _raise_operation_failure(
                    status_code=429,
                    code="group_style_rate_limited",
                    phase="rate_limit",
                    title="群风格重建过于频繁",
                    message="同一管理员设备在当前时间窗口内已达到群风格重建上限。",
                    details=(detail("目标群", group_id), detail("窗口秒数", _REBUILD_WINDOW_SECONDS), detail("窗口上限", _REBUILD_MAX_PER_WINDOW)),
                    steps=(step("rate_limit", "检查重建频率", "error", "当前时间窗口的重建额度已用尽。"),),
                    suggestion=f"等待约 {int(_REBUILD_WINDOW_SECONDS / 60)} 分钟后重试。",
                    retryable=True,
                    partial=False,
                    outcome_unknown=False,
                )
            bucket["count"] = int(bucket.get("count", 0) or 0) + 1
            rl_data[rate_key] = bucket
            store.save_sync(_REBUILD_RATELIMIT_NS, rl_data)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_style_rate_limit_failed",
                phase="rate_limit",
                title="群风格重建前置检查失败",
                message="服务器无法读取或更新重建频率状态，未开始模型调用。",
                suggestion="检查数据存储状态后重试。",
                details=(detail("目标群", group_id),),
                steps=(step("rate_limit", "检查重建频率", "error", "频率状态读写失败。"),),
                retryable=True,
            )

        from ...core.group_style_autobuild import build_group_style, _load_messages_since, _format_chat_summary

        store_mem = _memory_store(runtime)
        if store_mem is None:
            _raise_operation_failure(
                status_code=503,
                code="group_style_memory_unavailable",
                phase="preflight",
                title="群风格重建服务未就绪",
                message="群记忆存储当前不可用，无法读取风格样本。",
                details=(detail("目标群", group_id),),
                steps=(step("preflight", "检查群记忆存储", "error", "memory_store 不可用。"),),
                suggestion="等待插件运行时完成初始化后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        bundle = getattr(runtime, "runtime_bundle", None)
        deps = getattr(bundle, "reply_processor_deps", None) if bundle else None
        runtime_inner = getattr(deps, "runtime", None) if deps else None
        tool_caller = getattr(runtime_inner, "agent_tool_caller", None) if runtime_inner else None
        if tool_caller is None:
            _raise_operation_failure(
                status_code=503,
                code="group_style_caller_unavailable",
                phase="caller",
                title="群风格模型调用器未就绪",
                message="当前没有可用的 agent_tool_caller，尚未发起模型调用。",
                details=(detail("目标群", group_id),),
                steps=(
                    step("source", "读取群聊样本", "skipped", "模型调用器前置检查未通过。"),
                    step("caller", "调用群风格模型", "error", "agent_tool_caller 不可用。"),
                ),
                suggestion="检查 Provider 和 Agent runtime 状态后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        # 取最近 250 条对话强行喂给 LLM；跳过 daily_limit
        try:
            rows = _load_messages_since(memory_store=store_mem, group_id=group_id, since_ts=0, limit=250)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_style_source_failed",
                phase="source",
                title="群风格样本读取失败",
                message="服务器无法读取本群近期消息，尚未发起模型调用。",
                suggestion="检查群记忆数据库状态后重试。",
                details=(detail("目标群", group_id),),
                steps=(step("source", "读取群聊样本", "error", "群聊样本读取异常。"),),
                retryable=True,
            )
        if len(rows) < 20:
            _raise_operation_failure(
                status_code=400,
                code="group_style_sample_insufficient",
                phase="source",
                title="群风格样本不足",
                message="本群可用消息少于 20 条，未开始模型调用。",
                details=(detail("目标群", group_id), detail("可用消息", len(rows)), detail("最低消息数", 20)),
                steps=(
                    step("source", "读取群聊样本", "error", "可用消息数量未达到最低要求。"),
                    step("caller", "调用群风格模型", "skipped", "样本不足。"),
                ),
                suggestion="等待群内积累更多有效消息后再重建。",
                retryable=False,
                partial=False,
                outcome_unknown=False,
            )
        try:
            chat_summary = _format_chat_summary(rows)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_style_source_format_failed",
                phase="source",
                title="群风格样本整理失败",
                message="群聊样本已读取，但无法整理为模型输入。",
                suggestion="检查群消息数据结构后重试。",
                details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok")),
                steps=(step("source", "整理群聊样本", "error", "样本格式化异常。"),),
                retryable=True,
            )
        caller_probe = _ModelOutputProbe(tool_caller, output_kind="style")
        try:
            built = await build_group_style(
                tool_caller=caller_probe,
                memory_store=store_mem,
                group_id=group_id,
                chat_summary=chat_summary,
            )
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_style_persist_failed",
                phase="persistence",
                title="群风格快照保存失败",
                message="模型输出已通过结构校验，但快照写入没有得到明确结果。",
                suggestion="先刷新群风格快照；确认没有新增快照后再重试。",
                details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok")),
                steps=(
                    step("source", "整理群聊样本", "ok", "群聊样本已准备。"),
                    step("caller", "调用群风格模型", "ok", "已取得模型响应。"),
                    step("json", "解析风格 JSON", "ok", "JSON 对象可解析。"),
                    step("schema", "校验风格 schema", "ok", "五维风格结构有效。"),
                    step("persist", "保存群风格快照", "unknown", "快照写入异常，最终状态需要重新读取确认。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
        if not built:
            failure_kind = caller_probe.failure_kind or "empty"
            failure_specs = {
                "caller": (502, "group_style_caller_failed", "caller", "群风格模型调用失败", "模型调用未取得可用响应。"),
                "empty": (502, "group_style_empty_response", "model_output", "群风格模型返回空结果", "模型响应中没有可解析内容。"),
                "json": (422, "group_style_json_invalid", "json_parse", "群风格 JSON 无效", "模型响应不是有效的 JSON 对象。"),
                "schema": (422, "group_style_schema_invalid", "schema_validation", "群风格结构无效", "模型响应不符合五维群风格 schema。"),
            }
            status_code, code, phase, title, message = failure_specs[failure_kind]
            _record_audit(
                runtime,
                action="style_rebuild",
                qq=admin.qq,
                device_id=admin.device_id,
                target=group_id,
                outcome=code,
            )
            ordered_keys = ("caller", "empty", "json", "schema")
            failure_index = ordered_keys.index(failure_kind)
            model_steps = []
            labels = {
                "caller": ("caller", "调用群风格模型"),
                "empty": ("output", "检查模型输出"),
                "json": ("json", "解析风格 JSON"),
                "schema": ("schema", "校验风格 schema"),
            }
            for index, key in enumerate(ordered_keys):
                step_key, label = labels[key]
                if index < failure_index:
                    model_steps.append(step(step_key, label, "ok", "该阶段已通过。"))
                elif index == failure_index:
                    model_steps.append(step(step_key, label, "error", message))
                else:
                    model_steps.append(step(step_key, label, "skipped", "前一阶段未通过。"))
            _raise_operation_failure(
                status_code=status_code,
                code=code,
                phase=phase,
                title=title,
                message=message,
                details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok"), detail("快照写入", "not_started", "ok")),
                steps=(step("source", "整理群聊样本", "ok", "群聊样本已准备。"), *model_steps, step("persist", "保存群风格快照", "skipped", "模型输出未通过校验。")),
                suggestion="检查 Provider 输出格式后重试；本次没有保存新快照。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        from ...core.group_style_autobuild import list_style_snapshots

        if not isinstance(built, dict) or not _valid_group_style_schema(built.get("style_json")):
            _raise_operation_failure(
                status_code=422,
                code="group_style_schema_invalid",
                phase="schema_validation",
                title="群风格结构无效",
                message="群风格构建结果不符合稳定 response schema。",
                details=(detail("目标群", group_id), detail("快照写入", "unverified", "warn")),
                steps=(
                    step("caller", "调用群风格模型", "ok", "构建函数已返回。"),
                    step("schema", "校验风格 schema", "error", "返回结果缺少有效的五维 style_json。"),
                    step("persist", "确认群风格快照", "unknown", "无法根据返回结果确认快照状态。"),
                ),
                suggestion="刷新群风格快照并检查模型输出；确认状态前不要直接重试。",
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
        try:
            snapshots = list_style_snapshots(group_id, limit=3)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_style_persist_verify_failed",
                phase="persistence",
                title="群风格快照验证失败",
                message="构建函数已返回快照，但服务器无法重新读取快照列表。",
                suggestion="刷新群风格页面确认新增快照；确认状态前不要直接重建。",
                details=(detail("目标群", group_id), detail("返回快照 ID", built.get("id"), "ok")),
                steps=(
                    step("caller", "调用并校验群风格模型", "ok", "模型输出已通过校验。"),
                    step("persist", "保存群风格快照", "ok", "构建函数已返回快照 ID。"),
                    step("verify", "重新读取快照列表", "unknown", "快照列表读取异常。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
        snapshot_id = int(built.get("id", 0) or 0)
        if snapshot_id <= 0 or not any(int(item.get("id", 0) or 0) == snapshot_id for item in snapshots):
            _raise_operation_failure(
                status_code=500,
                code="group_style_persist_unverified",
                phase="persistence",
                title="群风格快照未能验证",
                message="构建函数返回后，快照列表中没有找到对应记录。",
                details=(detail("目标群", group_id), detail("返回快照 ID", snapshot_id, "warn")),
                steps=(
                    step("caller", "调用并校验群风格模型", "ok", "模型输出已通过校验。"),
                    step("persist", "保存群风格快照", "unknown", "无法确认返回快照已持久化。"),
                    step("verify", "重新读取快照列表", "error", "对应快照不存在。"),
                ),
                suggestion="检查数据库写入状态；确认快照不存在后再重试。",
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="style_rebuild",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            detail={"snapshot_id": built.get("id")},
        )
        return _operation_success(
            {"success": True, "new_snapshot": built, "snapshots": snapshots},
            code="group_style_rebuilt",
            phase="operation_complete",
            title="群风格已重建",
            message="模型输出已通过 JSON 与 schema 校验，新快照已写入并重新读取确认。",
            details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok"), detail("快照 ID", snapshot_id, "ok")),
            steps=(
                step("source", "整理群聊样本", "ok", "群聊样本已准备。"),
                step("caller", "调用群风格模型", "ok", "已取得模型响应。"),
                step("json", "解析风格 JSON", "ok", "JSON 对象可解析。"),
                step("schema", "校验风格 schema", "ok", "五维风格结构有效。"),
                step("persist", "保存并验证群风格快照", "ok", "新快照已持久化并可读取。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "快照已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("群风格快照已保存，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

    @router.get("/{group_id}/agent-state")
    async def agent_state(
        group_id: str,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """汇总群信息卡片需要的 agent 状态：情绪、最近记忆、关系 top 边、近期活跃。"""
        from ...core.emotion_state import (
            describe_group_emotion_memory,
            load_emotion_state,
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
                inner_state_hint = " / ".join(
                    value for value in (
                        str(loaded.get("mood", "") or "").strip(),
                        str(loaded.get("energy", "") or "").strip(),
                    ) if value
                )
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
            raise HTTPException(status_code=500, detail="群记忆读取失败") from exc
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
            raise HTTPException(status_code=500, detail="群知识读取失败") from exc
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
        try:
            ds = get_data_store()
            now_ts = time.time()
            rl_data = ds.load_sync(_KNOWLEDGE_REBUILD_RATELIMIT_NS) or {}
            if not isinstance(rl_data, dict):
                rl_data = {}
            bucket = rl_data.get(rate_key) if isinstance(rl_data.get(rate_key), dict) else {}
            if not isinstance(bucket, dict) or now_ts - float(bucket.get("window_start", 0) or 0) > _REBUILD_WINDOW_SECONDS:
                bucket = {"window_start": now_ts, "count": 0}
            if int(bucket.get("count", 0) or 0) >= _REBUILD_MAX_PER_WINDOW:
                _raise_operation_failure(
                    status_code=429,
                    code="group_knowledge_rate_limited",
                    phase="rate_limit",
                    title="群知识重建过于频繁",
                    message="同一管理员设备在当前时间窗口内已达到群知识重建上限。",
                    details=(detail("目标群", group_id), detail("窗口秒数", _REBUILD_WINDOW_SECONDS), detail("窗口上限", _REBUILD_MAX_PER_WINDOW)),
                    steps=(step("rate_limit", "检查重建频率", "error", "当前时间窗口的重建额度已用尽。"),),
                    suggestion=f"等待约 {int(_REBUILD_WINDOW_SECONDS / 60)} 分钟后重试。",
                    retryable=True,
                    partial=False,
                    outcome_unknown=False,
                )
            bucket["count"] = int(bucket.get("count", 0) or 0) + 1
            rl_data[rate_key] = bucket
            ds.save_sync(_KNOWLEDGE_REBUILD_RATELIMIT_NS, rl_data)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_knowledge_rate_limit_failed",
                phase="rate_limit",
                title="群知识重建前置检查失败",
                message="服务器无法读取或更新重建频率状态，未开始模型调用。",
                suggestion="检查数据存储状态后重试。",
                details=(detail("目标群", group_id),),
                steps=(step("rate_limit", "检查重建频率", "error", "频率状态读写失败。"),),
                retryable=True,
            )

        from ...core.group_knowledge import build_group_knowledge
        from ...core.group_knowledge_autobuild import _load_messages_since, _format_chat_summary

        store_mem = _memory_store(runtime)
        if store_mem is None:
            _raise_operation_failure(
                status_code=503,
                code="group_knowledge_memory_unavailable",
                phase="preflight",
                title="群知识重建服务未就绪",
                message="群记忆存储当前不可用，无法读取样本或保存知识。",
                details=(detail("目标群", group_id),),
                steps=(step("preflight", "检查群记忆存储", "error", "memory_store 不可用。"),),
                suggestion="等待插件运行时完成初始化后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        bundle = getattr(runtime, "runtime_bundle", None)
        deps = getattr(bundle, "reply_processor_deps", None) if bundle else None
        runtime_inner = getattr(deps, "runtime", None) if deps else None
        tool_caller = getattr(runtime_inner, "agent_tool_caller", None) if runtime_inner else None
        if tool_caller is None:
            _raise_operation_failure(
                status_code=503,
                code="group_knowledge_caller_unavailable",
                phase="caller",
                title="群知识模型调用器未就绪",
                message="当前没有可用的 agent_tool_caller，尚未发起模型调用。",
                details=(detail("目标群", group_id),),
                steps=(step("caller", "调用群知识模型", "error", "agent_tool_caller 不可用。"),),
                suggestion="检查 Provider 和 Agent runtime 状态后重试。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        try:
            rows = _load_messages_since(memory_store=store_mem, group_id=group_id, since_ts=0, limit=200)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_knowledge_source_failed",
                phase="source",
                title="群知识样本读取失败",
                message="服务器无法读取本群近期消息，尚未发起模型调用。",
                suggestion="检查群记忆数据库状态后重试。",
                details=(detail("目标群", group_id),),
                steps=(step("source", "读取群聊样本", "error", "群聊样本读取异常。"),),
                retryable=True,
            )
        if len(rows) < 20:
            _raise_operation_failure(
                status_code=400,
                code="group_knowledge_sample_insufficient",
                phase="source",
                title="群知识样本不足",
                message="本群可用消息少于 20 条，未开始模型调用。",
                details=(detail("目标群", group_id), detail("可用消息", len(rows)), detail("最低消息数", 20)),
                steps=(
                    step("source", "读取群聊样本", "error", "可用消息数量未达到最低要求。"),
                    step("caller", "调用群知识模型", "skipped", "样本不足。"),
                ),
                suggestion="等待群内积累更多有效消息后再重建。",
                retryable=False,
                partial=False,
                outcome_unknown=False,
            )
        try:
            chat_summary = _format_chat_summary(rows)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_knowledge_source_format_failed",
                phase="source",
                title="群知识样本整理失败",
                message="群聊样本已读取，但无法整理为模型输入。",
                suggestion="检查群消息数据结构后重试。",
                details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok")),
                steps=(step("source", "整理群聊样本", "error", "样本格式化异常。"),),
                retryable=True,
            )
        caller_probe = _ModelOutputProbe(tool_caller, output_kind="knowledge")
        persistence_probe = _KnowledgePersistenceProbe(store_mem)
        try:
            saved = await build_group_knowledge(
                tool_caller=caller_probe,
                memory_store=persistence_probe,
                group_id=group_id,
                chat_summary=chat_summary,
            )
        except Exception as exc:
            confirmed = persistence_probe.confirmed_writes
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_knowledge_persist_failed",
                phase="persistence",
                title="群知识保存失败",
                message="模型输出已通过结构校验，但知识条目未能全部保存。",
                suggestion="先刷新群知识列表并核对已保存条目；确认状态前不要直接重建。",
                details=(
                    detail("目标群", group_id),
                    detail("已确认写入", confirmed, "warn" if confirmed else "info"),
                    detail("最终写入状态", "unknown", "warn"),
                ),
                steps=(
                    step("source", "整理群聊样本", "ok", "群聊样本已准备。"),
                    step("caller", "调用并校验群知识模型", "ok", "模型输出已通过 JSON 与 schema 校验。"),
                    step("persist", "保存群知识条目", "unknown", "逐条保存异常，可能已有部分条目写入。"),
                ),
                retryable=False,
                partial=confirmed > 0,
                outcome_unknown=True,
            )
        saved_count = int(saved or 0)
        if saved_count <= 0:
            failure_kind = caller_probe.failure_kind or "empty"
            failure_specs = {
                "caller": (502, "group_knowledge_caller_failed", "caller", "群知识模型调用失败", "模型调用未取得可用响应。"),
                "empty": (502, "group_knowledge_empty_response", "model_output", "群知识模型返回空结果", "模型响应中没有可解析内容或知识数组为空。"),
                "json": (422, "group_knowledge_json_invalid", "json_parse", "群知识 JSON 无效", "模型响应不是有效的 JSON 数组。"),
                "schema": (422, "group_knowledge_schema_invalid", "schema_validation", "群知识结构无效", "模型响应中的知识条目不符合 schema。"),
            }
            status_code, code, phase, title, message = failure_specs[failure_kind]
            _record_audit(
                runtime,
                action="knowledge_rebuild",
                qq=admin.qq,
                device_id=admin.device_id,
                target=group_id,
                outcome=code,
                detail={"saved": 0},
            )
            _raise_operation_failure(
                status_code=status_code,
                code=code,
                phase=phase,
                title=title,
                message=message,
                details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok"), detail("已确认写入", 0, "ok")),
                steps=(
                    step("source", "整理群聊样本", "ok", "群聊样本已准备。"),
                    step("caller", "调用群知识模型", "error" if failure_kind == "caller" else "ok", message if failure_kind == "caller" else "已取得模型响应。"),
                    step("output", "解析并校验知识数组", "error" if failure_kind != "caller" else "skipped", message if failure_kind != "caller" else "模型调用失败。"),
                    step("persist", "保存群知识条目", "skipped", "没有通过校验的知识条目。"),
                ),
                suggestion="检查 Provider 输出格式后重试；本次没有确认写入新知识。",
                retryable=True,
                partial=False,
                outcome_unknown=False,
            )
        if saved_count != persistence_probe.confirmed_writes:
            _raise_operation_failure(
                status_code=500,
                code="group_knowledge_persist_unverified",
                phase="persistence",
                title="群知识保存结果无法验证",
                message="构建函数报告的保存数量与已确认写入数量不一致。",
                details=(
                    detail("目标群", group_id),
                    detail("报告保存数", saved_count, "warn"),
                    detail("已确认写入", persistence_probe.confirmed_writes, "warn"),
                ),
                steps=(
                    step("caller", "调用并校验群知识模型", "ok", "模型输出已通过校验。"),
                    step("persist", "保存群知识条目", "unknown", "保存计数无法一致验证。"),
                ),
                suggestion="刷新群知识列表并检查数据库写入状态；确认状态前不要直接重建。",
                retryable=False,
                partial=persistence_probe.confirmed_writes > 0,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="knowledge_rebuild",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"saved": saved_count},
        )
        return _operation_success(
            {"success": True, "saved": saved_count},
            code="group_knowledge_rebuilt",
            phase="operation_complete",
            title="群知识已重建",
            message="模型输出已通过 JSON 与 schema 校验，知识条目已逐条保存。",
            details=(detail("目标群", group_id), detail("可用消息", len(rows), "ok"), detail("已保存条目", saved_count, "ok")),
            steps=(
                step("source", "整理群聊样本", "ok", "群聊样本已准备。"),
                step("caller", "调用群知识模型", "ok", "已取得模型响应。"),
                step("schema", "解析并校验知识数组", "ok", "知识数组结构有效。"),
                step("persist", "保存群知识条目", "ok", "报告数量与确认写入数量一致。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "知识已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("群知识已保存，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

    @router.get("/{group_id}/memes")
    async def group_memes(
        group_id: str,
        limit: int = 1000,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            items = list_meme_entries(group_id=group_id, limit=max(1, min(int(limit), 2000)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail="群梗词典读取失败") from exc
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
        try:
            ok = upsert_meme_entry(payload)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_meme_persist_failed",
                phase="persistence",
                title="群梗词条保存失败",
                message="服务器未能确认群梗词条已保存。",
                suggestion="先刷新群梗词典确认当前词条；未生效时再重试。",
                details=(detail("目标群", group_id), detail("词条", payload.get("term", "")), detail("范围", scope)),
                steps=(
                    step("validate", "校验群梗词条", "ok", "词条已进入持久化阶段。"),
                    step("persist", "保存群梗词条", "unknown", "写入过程异常，最终状态需要重新读取确认。"),
                    step("audit", "记录管理员操作", "skipped", "持久化结果未知。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
        if not ok:
            _raise_operation_failure(
                status_code=400,
                code="group_meme_invalid",
                phase="request_validation",
                title="群梗词条未保存",
                message="term 和 meaning/definition 不能为空。",
                details=(detail("目标群", group_id), detail("范围", scope)),
                steps=(
                    step("validate", "校验群梗词条", "error", "缺少有效词条或含义。"),
                    step("persist", "保存群梗词条", "skipped", "未写入任何词条。"),
                ),
                suggestion="补充词条和含义后重新提交。",
                retryable=False,
                partial=False,
                outcome_unknown=False,
            )
        audit_ok = _record_audit(
            runtime,
            action="meme_upsert",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"term": payload.get("term"), "scope": scope},
        )
        return _operation_success(
            {"success": True, "entry": payload},
            code="group_meme_saved",
            phase="operation_complete",
            title="群梗词条已保存",
            message="群梗词条已持久化。",
            details=(detail("目标群", group_id), detail("词条", payload.get("term", ""), "ok"), detail("范围", scope)),
            steps=(
                step("validate", "校验群梗词条", "ok", "词条和含义有效。"),
                step("persist", "保存群梗词条", "ok", "群梗词条写入事务已提交。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "词条已保存，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("群梗词条已保存，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

    @router.delete("/{group_id}/memes/{term}")
    async def delete_group_meme(
        group_id: str,
        term: str,
        request: Request,
        scope: str = "group",
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        normalized_scope = scope if scope in {"group", "concept"} else "group"
        try:
            changed = delete_meme_entry(term=term, scope=normalized_scope, group_id=group_id)
        except Exception as exc:
            _raise_unexpected_failure(
                runtime,
                exc,
                status_code=500,
                code="group_meme_delete_failed",
                phase="persistence",
                title="群梗词条删除失败",
                message="服务器未能确认群梗词条已删除。",
                suggestion="先刷新群梗词典确认词条是否仍存在；存在时再重试。",
                details=(detail("目标群", group_id), detail("词条", term), detail("范围", normalized_scope)),
                steps=(
                    step("persist", "删除群梗词条", "unknown", "删除过程异常，最终状态需要重新读取确认。"),
                    step("audit", "记录管理员操作", "skipped", "持久化结果未知。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
        audit_ok = _record_audit(
            runtime,
            action="meme_delete",
            qq=admin.qq,
            device_id=admin.device_id,
            target=group_id,
            ip_hash=get_client_ip(request),
            detail={"term": term, "scope": normalized_scope, "changed": changed},
        )
        return _operation_success(
            {"success": True, "deleted": changed},
            code="group_meme_deleted" if changed else "group_meme_delete_noop",
            phase="operation_complete",
            title="群梗词条已删除" if changed else "群梗词条无需删除",
            message="群梗词条删除事务已提交。" if changed else "目标群梗词条当前不存在。",
            details=(detail("目标群", group_id), detail("词条", term), detail("范围", normalized_scope), detail("已删除", changed, "ok")),
            steps=(
                step("persist", "删除群梗词条", "ok", "删除结果已明确返回。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "删除结果已确认，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("删除结果已确认，但本次管理员审计记录未能写入。",),
            partial=not audit_ok,
        )

    return router
