from __future__ import annotations

import time
import uuid
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.operation_diagnostics import (
    OperationDetail,
    OperationStep,
    detail as operation_detail,
    diagnostic as operation_diagnostic,
    exception_diagnostic,
    step as operation_step,
)
from ..deps import AdminIdentity, require_admin


# 默认从长期记忆视图中排除的 "bot 自言/自我日志" 来源；
# 这些条目是 bot 自己回过的话，对管理员价值低，默认隐藏。
_SELF_LOG_SOURCE_KINDS: frozenset[str] = frozenset({
    "self_log",
    "self_reply",
    "self_say",
    "bot_say",
    "assistant_reply",
})

_SELF_LOG_MEMORY_TYPES: frozenset[str] = frozenset({
    "episodic",  # bot 视角的事件日志，绝大多数是它说过的话
})

_MEMORY_TYPE_LABELS: dict[str, str] = {
    "semantic": "长期语义",
    "fact": "事实记忆",
    "group_knowledge": "群知识",
    "group_meme": "群梗词典",
    "concept_anchor": "概念锚点",
    "user_persona": "用户画像",
    "persona_knowledge": "人设知识",
    "episodic": "事件片段",
    "episodic_turn": "对话回合",
    "conflict_memory": "冲突记忆",
}

_SOURCE_KIND_LABELS: dict[str, str] = {
    "user": "用户发言",
    "user_persona": "用户画像",
    "auto_extract": "自动抽取",
    "plugin": "插件记录",
    "image": "图片理解",
    "mface": "表情理解",
    "self_log": "Bot 自身记录",
    "self_reply": "Bot 回复记录",
    "assistant_reply": "Bot 回复记录",
    "bot_reply": "Bot 回复记录",
    "system": "系统记录",
}

_TIER_LABELS: dict[str, str] = {
    "working": "工作记忆",
    "short": "短期记忆",
    "long": "长期记忆",
    "core": "核心记忆",
    "archive": "归档记忆",
}

_NODE_KIND_LABELS: dict[str, str] = {
    "memory": "记忆条目",
    "entity": "实体/标签",
    "user": "群成员",
}

_ENTITY_TYPE_LABELS: dict[str, str] = {
    "tag": "标签",
    "external": "外部实体",
    "person": "人物",
    "user": "用户",
    "topic": "主题",
    "place": "地点",
    "item": "物品",
}

_RELATION_LABELS: dict[str, str] = {
    "tag": "标签关联",
    "related": "相关",
    "similar": "相似",
    "supports": "支持",
    "contradicts": "冲突",
    "same_topic": "同一话题",
    "same_user": "同一用户",
    "reply": "回复关系",
    "mention": "提及",
    "co_occurs": "共同出现",
    "talks_to": "对话",
    "reacts_to": "回应",
    "quotes": "引用",
}

_SEARCH_SOURCE_LABELS: dict[str, str] = {
    "fts": "全文检索",
    "vector": "向量检索",
    "exact": "精确匹配",
    "hybrid": "混合检索",
}


def _label(mapping: dict[str, str], value: Any, fallback: str) -> str:
    key = str(value or "").strip()
    if not key:
        return fallback
    return mapping.get(key, fallback)


def _decorate_memory_item(item: dict[str, Any]) -> dict[str, Any]:
    rendered = dict(item)
    rendered["memory_type_label"] = _label(_MEMORY_TYPE_LABELS, rendered.get("memory_type"), "其他记忆")
    rendered["source_kind_label"] = _label(_SOURCE_KIND_LABELS, rendered.get("source_kind"), "其他来源")
    rendered["tier_label"] = _label(_TIER_LABELS, rendered.get("tier"), "未分层")
    zone = str(rendered.get("palace_zone", "") or "").strip()
    rendered["palace_zone_label"] = zone or "未分区"
    search_source = str(rendered.get("search_source", "") or "").strip()
    if search_source:
        rendered["search_source_label"] = _label(_SEARCH_SOURCE_LABELS, search_source, "其他检索")
    return rendered


def _relation_label(kind: Any) -> str:
    return _label(_RELATION_LABELS, kind, "其他关联")


def _memory_store(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "memory_store", None)


def _operation_result(report: dict[str, Any], **fields: Any) -> dict[str, Any]:
    result = dict(report)
    result.update(fields)
    result["diagnostic"] = dict(report)
    return result


def _raise_operation(status_code: int, report: dict[str, Any]) -> NoReturn:
    raise HTTPException(status_code=status_code, detail=report)


def _exception_report(
    exc: BaseException,
    *,
    runtime: Any,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    operation_id: str = "",
    details: tuple[OperationDetail, ...] = (),
    steps: tuple[OperationStep, ...] = (),
    retryable: bool | None = None,
    partial: bool = False,
    outcome_unknown: bool = False,
) -> dict[str, Any]:
    report = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message=message,
        suggestion=suggestion,
        operation_id=operation_id,
        retryable=retryable,
    )
    report["code"] = code
    report["partial"] = bool(partial)
    report["outcome_unknown"] = bool(outcome_unknown)
    report["details"] = [item.to_dict() for item in details] + report.get("details", [])
    report["steps"] = [item.to_dict() for item in steps]
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.warning(
            f"[webui] memory operation failed: code={code} "
            f"exception={type(exc).__name__} trace={report.get('trace_id', '')}"
        )
    return report


def _store_unavailable_report(*, operation_id: str = "", mutation: bool = False) -> dict[str, Any]:
    return operation_diagnostic(
        ok=False,
        code="memory_store_unavailable",
        phase="dependency",
        title="MemoryStore 未就绪",
        message="当前 runtime 没有可用的 MemoryStore。",
        details=(operation_detail("MemoryStore", "unavailable", "error"),),
        steps=(operation_step("store", "连接 MemoryStore", "error", "未取得记忆存储实例。"),),
        suggestion=(
            "确认 memory palace 已启用且 runtime 初始化完成；核对当前状态后再重试写操作。"
            if mutation
            else "确认 memory palace 已启用且 runtime 初始化完成后刷新页面。"
        ),
        retryable=True,
        partial=False,
        outcome_unknown=False,
        operation_id=operation_id,
    )


def _private_api_unavailable_report(*, operation_id: str = "", purpose: str) -> dict[str, Any]:
    return operation_diagnostic(
        ok=False,
        code="memory_private_store_api_unavailable",
        phase="dependency",
        title="MemoryStore 私有接口不可用",
        message="当前版本无法安全访问该记忆存储能力。",
        details=(operation_detail("用途", purpose, "error"),),
        steps=(operation_step("private_api", "加载 MemoryStore 私有接口", "error", "依赖接口不可用。"),),
        suggestion="确认插件文件版本一致并重启 runtime；不要绕过接口直接修改数据库。",
        retryable=False,
        partial=False,
        outcome_unknown=False,
        operation_id=operation_id,
    )


def _looks_like_bot_self_entry(item: dict[str, Any]) -> bool:
    source_kind = str(item.get("source_kind", "") or "").lower()
    memory_type = str(item.get("memory_type", "") or "").lower()
    if source_kind in _SELF_LOG_SOURCE_KINDS:
        return True
    if memory_type in _SELF_LOG_MEMORY_TYPES:
        return True
    return False

def build_memory_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/memory", tags=["memory"])

    @router.get("/vector-index")
    async def vector_index_status(_: AdminIdentity = Depends(require_admin)) -> dict:
        store = _memory_store(runtime)
        if store is None:
            return {"available": False, "reason": "memory_store_missing"}
        try:
            return {"available": True, **dict(store.get_vector_index_status())}
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_vector_status_failed",
                phase="status_read",
                title="无法读取记忆向量索引状态",
                message="服务器读取索引统计时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志；存储恢复后刷新页面。",
                steps=(operation_step("status", "读取向量索引状态", "error", "未取得可靠统计。"),),
            )
            _raise_operation(500, report)

    @router.post("/vector-index/rebuild")
    async def rebuild_vector_index(
        limit: int = Query(default=0, ge=0, le=10000),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        operation_id = uuid.uuid4().hex
        store = _memory_store(runtime)
        if store is None:
            _raise_operation(503, _store_unavailable_report(operation_id=operation_id, mutation=True))
        try:
            result = dict(store.rebuild_vector_index(limit=int(limit or 0)))
            verified_index = dict(store.get_vector_index_status())
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_vector_rebuild_unconfirmed",
                phase="persistence_verification",
                title="向量索引重建结果未确认",
                message="索引重建或重建后的状态核验未明确完成。",
                suggestion="刷新索引状态并核对待补建数量；确认状态后再决定是否重试。",
                operation_id=operation_id,
                details=(operation_detail("重建上限", int(limit or 0), "info"),),
                steps=(
                    operation_step("rebuild", "批量重建向量索引", "unknown", "批量写入可能已部分或全部提交。"),
                    operation_step("verify", "读取索引状态核验", "unknown", "未取得可靠的持久化状态。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
            _raise_operation(500, report)
        result["index"] = verified_index
        rebuilt = int(result.get("rebuilt", 0) or 0)
        disabled = str(result.get("status", "") or "").lower() == "disabled"
        report = operation_diagnostic(
            ok=True,
            code="memory_vector_rebuild_disabled" if disabled else "memory_vector_rebuilt",
            phase="operation_complete",
            title="向量索引未启用" if disabled else "向量索引已重建",
            message="当前向量索引未启用，没有写入索引。" if disabled else f"已重建 {rebuilt} 条记忆索引并核验当前状态。",
            details=(
                operation_detail("重建条数", rebuilt, "info" if disabled else "ok"),
                operation_detail("待补建", int(verified_index.get("stale_count", 0) or 0), "info"),
            ),
            steps=(
                operation_step("rebuild", "批量重建向量索引", "skipped" if disabled else "ok", "索引未启用。" if disabled else "批量写入已完成。"),
                operation_step("verify", "读取索引状态核验", "ok", "已取得持久化后的索引统计。"),
            ),
            warnings=("personification_memory_rag_enabled 或向量 backend 当前未启用。",) if disabled else (),
            suggestion="启用 RAG 后再重建。" if disabled else "无需立即重复重建。",
            operation_id=operation_id,
        )
        return _operation_result(report, **result)

    @router.get("/search-test")
    async def search_test(
        query: str = Query(default="", min_length=1, max_length=300),
        group_id: str = Query(default=""),
        user_id: str = Query(default=""),
        context_type: str = Query(default="auto"),
        limit: int = Query(default=8, ge=1, le=32),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _memory_store(runtime)
        if store is None:
            _raise_operation(503, _store_unavailable_report(mutation=True))
        try:
            items = list(
                store.recall_memories(
                    query=str(query or "").strip(),
                    group_id=str(group_id or "").strip(),
                    user_id=str(user_id or "").strip(),
                    context_type=str(context_type or "auto").strip() or "auto",
                    limit=int(limit or 8),
                )
            )
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_recall_test_unconfirmed",
                phase="recall_persistence",
                title="记忆召回测试结果未确认",
                message="召回过程可能已更新访问计数或搜索统计，但未返回完整结果。",
                suggestion="不要立即重复测试；先根据 Trace ID 检查脱敏日志并刷新记忆详情。",
                details=(
                    operation_detail("group_id", str(group_id or "").strip() or "-", "info"),
                    operation_detail("user_id", str(user_id or "").strip() or "-", "info"),
                ),
                steps=(
                    operation_step("recall", "执行记忆召回", "unknown", "召回未完整返回。"),
                    operation_step("stats", "更新访问与搜索统计", "unknown", "统计写入结果无法确认。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
            _raise_operation(500, report)
        rendered = []
        for item in items:
            rendered.append(
                {
                    "memory_id": str(item.get("memory_id", "") or ""),
                    "summary": str(item.get("summary", "") or "")[:300],
                    "memory_type": str(item.get("memory_type", "") or ""),
                    "memory_type_label": _label(_MEMORY_TYPE_LABELS, item.get("memory_type"), "其他记忆"),
                    "palace_zone": str(item.get("palace_zone", "") or ""),
                    "palace_zone_label": str(item.get("palace_zone", "") or "").strip() or "未分区",
                    "score": float(item.get("score", 0) or 0),
                    "search_source": str(item.get("search_source", "") or ""),
                    "search_source_label": _label(_SEARCH_SOURCE_LABELS, item.get("search_source"), "其他检索"),
                    "why_relevant": str(item.get("why_relevant", "") or ""),
                    "group_id": str(item.get("group_id", "") or ""),
                    "user_id": str(item.get("user_id", "") or ""),
                }
            )
        report = operation_diagnostic(
            ok=True,
            code="memory_recall_test_completed",
            phase="operation_complete",
            title="记忆召回测试已完成",
            message=f"召回返回 {len(rendered)} 条结果，并完成本次访问/搜索统计。",
            details=(operation_detail("结果数", len(rendered), "ok"),),
            steps=(
                operation_step("recall", "执行记忆召回", "ok", "召回结果已返回。"),
                operation_step("stats", "更新访问与搜索统计", "ok", "MemoryStore 未报告统计写入异常。"),
            ),
            suggestion="可在下方结果中核对相关性和检索来源。",
        )
        return _operation_result(report, items=rendered, query=query, count=len(rendered))

    @router.get("/recent")
    async def recent(
        limit: int = Query(default=100, ge=1, le=500),
        memory_type: str = Query(default=""),
        group_id: str = Query(default=""),
        user_id: str = Query(default=""),
        palace_zone: str = Query(default=""),
        source_kind: str = Query(default=""),
        include_self: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _memory_store(runtime)
        if store is None:
            return {"items": [], "palace_enabled": False}
        try:
            palace_on = bool(store.palace_enabled())
        except Exception:
            palace_on = False
        if not palace_on:
            return {"items": [], "palace_enabled": False}
        # 默认拉略多一些，过滤 bot 自言后剩下的可能少于 limit
        raw_limit = int(limit) if include_self else min(int(limit) * 3, 1000)
        try:
            items = list(store.list_recent_memories(
                group_id=str(group_id or "").strip(),
                user_id=str(user_id or "").strip(),
                palace_zone=str(palace_zone or "").strip(),
                limit=raw_limit,
                memory_type=str(memory_type or "").strip(),
                source_kind=str(source_kind or "").strip(),
            ))
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_recent_read_failed",
                phase="memory_read",
                title="无法读取近期记忆",
                message="服务器查询近期记忆时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志并刷新页面。",
                steps=(operation_step("read", "查询近期记忆", "error", "未取得可靠列表。"),),
            )
            _raise_operation(500, report)
        rendered = []
        hidden = 0
        for item in items:
            if not include_self and _looks_like_bot_self_entry(item):
                hidden += 1
                continue
            rendered.append(_decorate_memory_item({
                "memory_id": str(item.get("memory_id", "") or ""),
                "memory_type": str(item.get("memory_type", "") or ""),
                "group_id": str(item.get("group_id", "") or ""),
                "user_id": str(item.get("user_id", "") or ""),
                "summary": str(item.get("summary", "") or "")[:300],
                "source_kind": str(item.get("source_kind", "") or ""),
                "tier": str(item.get("tier", "") or ""),
                "palace_zone": str(item.get("palace_zone", "") or ""),
                "confidence": float(item.get("confidence", 0) or 0),
                "salience": float(item.get("salience", 0) or 0),
                "updated_at": float(item.get("updated_at", 0) or 0),
            }))
            if len(rendered) >= int(limit):
                break
        return {
            "items": rendered,
            "palace_enabled": True,
            "hidden_self_count": hidden,
            "include_self": bool(include_self),
        }

    @router.get("/raw-chat")
    async def raw_chat(
        group_id: str = Query(default=""),
        limit: int = Query(default=80, ge=1, le=300),
        before_ts: float = Query(default=0.0),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """读 chat_history.db 的群对话原文（未蒸馏），按时间倒序返回。"""
        store = _memory_store(runtime)
        if store is None:
            return {"messages": [], "available": False}
        gid = str(group_id or "").strip()
        if not gid:
            report = operation_diagnostic(
                ok=False,
                code="memory_raw_chat_group_required",
                phase="validation",
                title="缺少群 ID",
                message="读取群对话原文必须提供 group_id。",
                steps=(operation_step("validate", "校验 group_id", "error", "group_id 为空。"),),
                suggestion="选择一个群后重新打开对话原文。",
                retryable=True,
            )
            _raise_operation(400, report)
        try:
            from ...core.memory_store import _connect, _json_loads
        except Exception:
            _raise_operation(503, _private_api_unavailable_report(purpose="读取群对话原文"))
        try:
            group_dir = store.ensure_group_space(gid)
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_raw_chat_space_failed",
                phase="group_space",
                title="无法访问群记忆目录",
                message="服务器无法确认群对话数据库路径。",
                suggestion="根据 Trace ID 检查脱敏日志和数据目录权限。",
                details=(operation_detail("group_id", gid, "info"),),
                steps=(operation_step("group_space", "解析群记忆目录", "error", "未取得可用路径。"),),
            )
            _raise_operation(500, report)
        db_path = group_dir / "chat_history.db"
        if not db_path.exists():
            return {"messages": [], "available": True, "group_id": gid}
        cutoff = float(before_ts) if before_ts and before_ts > 0 else time.time() + 1
        try:
            with _connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT role, content, metadata, created_at
                    FROM messages
                    WHERE created_at < ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (cutoff, int(limit)),
                ).fetchall()
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_raw_chat_read_failed",
                phase="database_read",
                title="无法读取群对话原文",
                message="服务器查询 chat_history.db 时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志和数据库完整性。",
                details=(operation_detail("group_id", gid, "info"),),
                steps=(operation_step("read", "查询群对话原文", "error", "数据库查询未完成。"),),
            )
            _raise_operation(500, report)
        messages: list[dict[str, Any]] = []
        for row in rows:
            content = _json_loads(row["content"], row["content"])
            metadata = _json_loads(row["metadata"], {})
            text = ""
            if isinstance(content, list):
                parts = [str(p.get("text", "")).strip() for p in content if isinstance(p, dict)]
                text = " ".join(p for p in parts if p)
            elif isinstance(content, dict):
                text = str(content.get("text", "") or content.get("content", "")).strip()
            else:
                text = str(content or "").strip()
            messages.append({
                "role": str(row["role"] or ""),
                "user_id": str(metadata.get("user_id", "") if isinstance(metadata, dict) else ""),
                "sender_name": str(metadata.get("nickname", "") or metadata.get("sender_name", "") if isinstance(metadata, dict) else ""),
                "text": text[:500],
                "created_at": float(row["created_at"] or 0),
            })
        return {
            "messages": messages,
            "available": True,
            "group_id": gid,
            "next_before_ts": messages[-1]["created_at"] if messages else 0,
        }

    @router.get("/inner-state")
    async def inner_state_view(_: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            from ...agent.inner_state import load_inner_state
            from ...core.paths import get_data_dir

            data_dir = get_data_dir(getattr(runtime, "plugin_config", None))
            data = await load_inner_state(data_dir)
            return {"available": True, "state": data}
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_inner_state_read_failed",
                phase="state_read",
                title="无法读取 Inner State",
                message="服务器读取持久化 Inner State 时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志和状态文件。",
                steps=(operation_step("read", "读取 Inner State", "error", "未取得可靠状态。"),),
            )
            _raise_operation(500, report)

    @router.get("/detail/{memory_id}")
    async def detail(memory_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        store = _memory_store(runtime)
        if store is None:
            _raise_operation(503, _store_unavailable_report())
        try:
            item = store.get_memory_item(memory_id)
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_detail_read_failed",
                phase="memory_read",
                title="无法读取记忆详情",
                message="服务器读取目标记忆时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志并刷新列表。",
                details=(operation_detail("记忆 ID", memory_id, "info"),),
                steps=(operation_step("read", "读取记忆详情", "error", "未取得可靠内容。"),),
            )
            _raise_operation(500, report)
        if not isinstance(item, dict) or not item:
            report = operation_diagnostic(
                ok=False,
                code="memory_not_found",
                phase="memory_read",
                title="找不到记忆",
                message="目标记忆不存在或已被删除。",
                details=(operation_detail("记忆 ID", memory_id, "error"),),
                steps=(operation_step("read", "读取记忆详情", "error", "目标不存在。"),),
                suggestion="返回列表并刷新当前记忆数据。",
                retryable=False,
            )
            _raise_operation(404, report)
        related: list[dict[str, Any]] = []
        try:
            related = list(store.list_related_memory_candidates(memory_id=memory_id, limit=8))
        except Exception:
            related = []
        decorated_related = [_decorate_memory_item(r) for r in related if isinstance(r, dict)]
        return {"memory_id": memory_id, "item": _decorate_memory_item(item), "related": decorated_related}

    @router.get("/graph")
    async def memory_graph(
        group_id: str = Query(default=""),
        limit: int = Query(default=80, ge=10, le=300),
        min_salience: float = Query(default=0.0, ge=0.0, le=1.0),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """记忆宫殿力导向图数据：合并 memory_items / memory_entities /
        group_relation_edges，返回 {nodes, edges} 给前端 Cytoscape 渲染。"""
        store = _memory_store(runtime)
        if store is None:
            return {"nodes": [], "edges": [], "available": False}
        try:
            palace_on = bool(store.palace_enabled())
        except Exception:
            palace_on = False
        if not palace_on:
            return {"nodes": [], "edges": [], "available": False, "reason": "palace_disabled"}

        try:
            from ...core.memory_store import _connect
        except Exception:
            _raise_operation(503, _private_api_unavailable_report(purpose="读取记忆宫殿图谱"))

        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []

        # ---- 1) 记忆条目 → memory 节点
        palace_path = store.memory_palace_dir / "memory_palace.db"
        try:
            with _connect(palace_path) as conn:
                params: list[Any] = []
                where = ["1=1"]
                if group_id.strip():
                    where.append("group_id=?")
                    params.append(str(group_id).strip())
                if min_salience > 0:
                    where.append("salience >= ?")
                    params.append(float(min_salience))
                params.append(int(limit))
                rows = conn.execute(
                    f"""
                    SELECT memory_id, memory_type, group_id, user_id, summary,
                           salience, confidence, updated_at, palace_zone
                    FROM memory_items
                    WHERE {' AND '.join(where)}
                    ORDER BY salience DESC, updated_at DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
                memory_ids: list[str] = []
                for row in rows:
                    mid = str(row["memory_id"] or "")
                    if not mid:
                        continue
                    memory_ids.append(mid)
                    nodes[f"m:{mid}"] = {
                        "id": f"m:{mid}",
                        "kind": "memory",
                        "kind_label": _NODE_KIND_LABELS["memory"],
                        "memory_type": str(row["memory_type"] or ""),
                        "memory_type_label": _label(_MEMORY_TYPE_LABELS, row["memory_type"], "其他记忆"),
                        "label": (str(row["summary"] or "")[:40] or mid)[:40],
                        "salience": float(row["salience"] or 0),
                        "confidence": float(row["confidence"] or 0),
                        "updated_at": float(row["updated_at"] or 0),
                        "palace_zone": str(row["palace_zone"] or ""),
                        "palace_zone_label": str(row["palace_zone"] or "").strip() or "未分区",
                        "group_id": str(row["group_id"] or ""),
                        "user_id": str(row["user_id"] or ""),
                    }
                # ---- 2) 实体 → entity 节点（限定到上面拉到的 memory_ids，避免跨 group 噪音）
                if memory_ids:
                    placeholder = ",".join("?" * len(memory_ids))
                    ent_rows = conn.execute(
                        f"""
                        SELECT entity, memory_id, entity_type, weight
                        FROM memory_entities
                        WHERE memory_id IN ({placeholder})
                        """,
                        tuple(memory_ids),
                    ).fetchall()
                    for row in ent_rows:
                        ent = str(row["entity"] or "").strip()
                        mid = str(row["memory_id"] or "")
                        if not ent or not mid:
                            continue
                        ent_id = f"e:{ent}"
                        if ent_id not in nodes:
                            nodes[ent_id] = {
                                "id": ent_id,
                                "kind": "entity",
                                "kind_label": _NODE_KIND_LABELS["entity"],
                                "entity_type": str(row["entity_type"] or "tag"),
                                "entity_type_label": _label(_ENTITY_TYPE_LABELS, row["entity_type"], "实体"),
                                "label": ent[:24],
                                "weight": float(row["weight"] or 0),
                            }
                        edges.append(
                            {
                                "src": ent_id,
                                "dst": f"m:{mid}",
                                "kind": "tag",
                                "kind_label": _relation_label("tag"),
                                "weight": float(row["weight"] or 1),
                            }
                        )
                    # ---- 3) 记忆之间的 relation 边
                    rel_rows = conn.execute(
                        f"""
                        SELECT source_memory_id, target_ref, relation_type, weight
                        FROM memory_relations
                        WHERE source_memory_id IN ({placeholder})
                        """,
                        tuple(memory_ids),
                    ).fetchall()
                    for row in rel_rows:
                        src_mid = f"m:{row['source_memory_id']}"
                        tgt_ref = str(row["target_ref"] or "").strip()
                        if not tgt_ref:
                            continue
                        # target_ref 可能是另一个 memory_id 也可能是实体名
                        if tgt_ref in memory_ids:
                            tgt_id = f"m:{tgt_ref}"
                        else:
                            tgt_id = f"e:{tgt_ref}"
                            nodes.setdefault(
                                tgt_id,
                                {
                                    "id": tgt_id,
                                    "kind": "entity",
                                    "kind_label": _NODE_KIND_LABELS["entity"],
                                    "entity_type": "external",
                                    "entity_type_label": _ENTITY_TYPE_LABELS["external"],
                                    "label": tgt_ref[:24],
                                    "weight": 0.0,
                                },
                            )
                        edges.append(
                            {
                                "src": src_mid,
                                "dst": tgt_id,
                                "kind": str(row["relation_type"] or "related"),
                                "kind_label": _relation_label(row["relation_type"]),
                                "weight": float(row["weight"] or 0),
                            }
                        )
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_graph_read_failed",
                phase="database_read",
                title="无法读取记忆宫殿图谱",
                message="服务器查询记忆节点或关系时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志和 memory_palace.db。",
                details=(operation_detail("group_id", str(group_id or "").strip() or "-", "info"),),
                steps=(operation_step("read", "查询记忆节点与关系", "error", "图谱数据未完整返回。"),),
            )
            _raise_operation(500, report)

        # ---- 4) 群关系图：user-user 边（只在 group_id 明确时才加）
        if group_id.strip():
            try:
                from ...core.db import connect_sync
                from ...core.group_relation_edges import _decayed_weight
                import time as _time

                now_ts = _time.time()
                with connect_sync() as conn:
                    rel_rows = conn.execute(
                        """
                        SELECT src_user_id, dst_user_id, edge_kind, weight, last_seen_at
                        FROM group_relation_edges
                        WHERE group_id=?
                        ORDER BY last_seen_at DESC
                        LIMIT 200
                        """,
                        (str(group_id).strip(),),
                    ).fetchall()
                for row in rel_rows:
                    w = _decayed_weight(float(row["weight"] or 0), float(row["last_seen_at"] or 0), now_ts=now_ts)
                    if w <= 0.15:
                        continue
                    src = str(row["src_user_id"] or "")
                    dst = str(row["dst_user_id"] or "")
                    if not src or not dst:
                        continue
                    src_id = f"u:{src}"
                    dst_id = f"u:{dst}"
                    nodes.setdefault(
                        src_id,
                        {"id": src_id, "kind": "user", "kind_label": _NODE_KIND_LABELS["user"], "label": src, "weight": 0.0},
                    )
                    nodes.setdefault(
                        dst_id,
                        {"id": dst_id, "kind": "user", "kind_label": _NODE_KIND_LABELS["user"], "label": dst, "weight": 0.0},
                    )
                    edges.append(
                        {
                            "src": src_id,
                            "dst": dst_id,
                            "kind": str(row["edge_kind"] or "related"),
                            "kind_label": _relation_label(row["edge_kind"]),
                            "weight": round(w, 2),
                        }
                    )
            except Exception:
                pass

        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "available": True,
            "group_id": group_id,
            "limit": limit,
        }

    @router.get("/palace-zones")
    async def palace_zones(_: AdminIdentity = Depends(require_admin)) -> dict:
        store = _memory_store(runtime)
        if store is None:
            return {"zones": [], "available": False}
        try:
            from ...core.memory_store import _connect

            db_path = store.memory_palace_dir / "memory_palace.db"
        except Exception:
            _raise_operation(503, _private_api_unavailable_report(purpose="读取记忆分区"))
        if not db_path.exists():
            return {"zones": [], "available": True}
        try:
            with _connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT palace_zone FROM memory_items WHERE palace_zone IS NOT NULL AND palace_zone != '' ORDER BY palace_zone"
                ).fetchall()
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="memory_zones_read_failed",
                phase="database_read",
                title="无法读取记忆分区",
                message="服务器查询 palace_zone 时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志和 memory_palace.db。",
                steps=(operation_step("read", "查询记忆分区", "error", "未取得可靠分区列表。"),),
            )
            _raise_operation(500, report)
        zones = [str(row[0]) for row in rows if row and row[0]]
        return {"zones": zones, "available": True}

    return router
