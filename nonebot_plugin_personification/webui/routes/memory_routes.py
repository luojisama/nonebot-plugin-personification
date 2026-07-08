from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

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
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/vector-index/rebuild")
    async def rebuild_vector_index(
        limit: int = Query(default=0, ge=0, le=10000),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _memory_store(runtime)
        if store is None:
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
        try:
            return dict(store.rebuild_vector_index(limit=int(limit or 0)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

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
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
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
            raise HTTPException(status_code=500, detail=str(exc))
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
        return {"items": rendered, "query": query, "count": len(rendered)}

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
            raise HTTPException(status_code=500, detail=str(exc))
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
            raise HTTPException(status_code=400, detail="group_id 必填")
        try:
            from ...core.memory_store import _connect, _json_loads
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"memory_store 私有接口不可用：{exc}")
        try:
            group_dir = store.ensure_group_space(gid)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
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
            raise HTTPException(status_code=500, detail=str(exc))
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
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/detail/{memory_id}")
    async def detail(memory_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        store = _memory_store(runtime)
        if store is None:
            raise HTTPException(status_code=503, detail="memory_store 未就绪")
        try:
            item = store.get_memory_item(memory_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        if not isinstance(item, dict) or not item:
            raise HTTPException(status_code=404, detail=f"找不到记忆 {memory_id}")
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

        from ...core.memory_store import _connect

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
            raise HTTPException(status_code=500, detail=str(exc))

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
            return {"zones": [], "available": False}
        if not db_path.exists():
            return {"zones": [], "available": True}
        try:
            with _connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT palace_zone FROM memory_items WHERE palace_zone IS NOT NULL AND palace_zone != '' ORDER BY palace_zone"
                ).fetchall()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        zones = [str(row[0]) for row in rows if row and row[0]]
        return {"zones": zones, "available": True}

    return router
