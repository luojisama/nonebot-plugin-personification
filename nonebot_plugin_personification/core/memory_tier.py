"""记忆分层（P4 完整版）：

把原本只有"会衰减直至硬删"的扁平记忆，分成四个 tier 流转：

- `working`   ：≤24h 内的新事件，可被快速召回，也是衰减最快的一档。
- `episodic`  ：>24h 仍未被巩固的事件，正常衰减；超过 30 天且无 reinforcement
  会被 memory_decay 硬删。
- `semantic`  ：经过摘要 / 多次 reinforcement / 多次 access 之后的"事实型"记忆，
  decay 给底线值兜底，原则上不再被硬删。
- `background`：被摘要覆盖过的原始事件，保留但不再频繁参与召回；
  也享受底线值保护，不会被 decay 删掉。

数据上：tier 写入 memory_item.payload['tier']，不动 SQLite schema。
现有 palace_zone（person / group / topic / self / future / recent_episode / working）
作为"语义分类"继续保留，不与 tier 冲突。
"""

from __future__ import annotations

import time
from typing import Any, Iterable

# tier 常量；其他模块应当只通过 import 引用这些字符串
TIER_WORKING = "working"
TIER_EPISODIC = "episodic"
TIER_SEMANTIC = "semantic"
TIER_BACKGROUND = "background"

ALL_TIERS: tuple[str, ...] = (TIER_WORKING, TIER_EPISODIC, TIER_SEMANTIC, TIER_BACKGROUND)

# memory_type → 默认 tier（写入时 / 迁移时使用）。
# 摘要 / 知识 / 画像类型直接落在 semantic：它们本身就是"已提炼"的产物。
_SEMANTIC_MEMORY_TYPES: frozenset[str] = frozenset(
    {
        "session_summary",
        "daily_summary",
        "group_knowledge",
        "group_meme",
        "concept_anchor",
        "user_persona",
        "persona_knowledge",
        "core_profile",
        "fact",
        "semantic",
    }
)

# 关系图边的演化记录，归属 semantic（它是被 LLM 评估出的"演化结论"，不是原始 episode）
_SEMANTIC_MEMORY_TYPES_EXT: frozenset[str] = _SEMANTIC_MEMORY_TYPES | {"group_relation"}

# tier 晋升阈值
_PROMOTE_REINFORCEMENT = 3
_PROMOTE_ACCESS_COUNT = 5
_WORKING_TO_EPISODIC_SECONDS = 24 * 3600  # 24h


def classify_initial_tier(payload: dict[str, Any], *, now_ts: float | None = None) -> str:
    """决定一条新写入 / 待迁移记忆的初始 tier。"""
    memory_type = str(payload.get("memory_type", "") or "").strip().lower()
    if memory_type in _SEMANTIC_MEMORY_TYPES_EXT:
        return TIER_SEMANTIC
    reinforcement = int(payload.get("reinforcement_count", 0) or 0)
    access_count = int(payload.get("access_count", 0) or 0)
    if reinforcement >= _PROMOTE_REINFORCEMENT or access_count >= _PROMOTE_ACCESS_COUNT:
        return TIER_SEMANTIC
    now = float(now_ts if now_ts is not None else time.time())
    created = float(payload.get("time_created", 0) or 0)
    age_seconds = max(0.0, now - created) if created > 0 else 0.0
    if age_seconds <= _WORKING_TO_EPISODIC_SECONDS:
        return TIER_WORKING
    return TIER_EPISODIC


def should_promote(payload: dict[str, Any]) -> str | None:
    """检查一条 episodic / working 记忆是否应当被晋升。返回目标 tier 或 None。"""
    current = str(payload.get("tier", "") or "").strip().lower()
    if current == TIER_SEMANTIC or current == TIER_BACKGROUND:
        return None
    memory_type = str(payload.get("memory_type", "") or "").strip().lower()
    if memory_type in _SEMANTIC_MEMORY_TYPES_EXT:
        return TIER_SEMANTIC
    reinforcement = int(payload.get("reinforcement_count", 0) or 0)
    access_count = int(payload.get("access_count", 0) or 0)
    if reinforcement >= _PROMOTE_REINFORCEMENT or access_count >= _PROMOTE_ACCESS_COUNT:
        return TIER_SEMANTIC
    # working → episodic 由 classify_initial_tier 处理；这里不做时间晋升。
    return None


def is_protected_tier(payload: dict[str, Any]) -> bool:
    """memory_decay 用：判断当前条目是否处于受保护的 tier（不被硬删）。"""
    tier = str(payload.get("tier", "") or "").strip().lower()
    return tier in (TIER_SEMANTIC, TIER_BACKGROUND)


def reinforce_originals(
    memory_store: Any,
    *,
    group_id: str,
    since_ts: float,
    until_ts: float,
    triggered_by: str = "",
) -> int:
    """summarizer 写入 summary 后调用：

    - 为 [since_ts, until_ts] 窗口内同 group 的非 summary 条目 reinforcement_count += 1。
    - 同时把这些原始条目的 tier 标为 background（不再硬删但也不参与频繁召回）。
    - 返回受影响条数。

    设计：直接 SQL 更新 + 重新 normalize payload tier；不走 write_memory_item
    （会触发 revision 自增和搜索索引重建），避免 N 条记忆触发 N 次 FTS 重写。
    """
    if memory_store is None or not group_id:
        return 0
    try:
        palace_on = bool(memory_store.palace_enabled())
    except Exception:
        palace_on = False
    if not palace_on:
        return 0
    try:
        from .memory_store import _connect, _json_loads
    except Exception:
        return 0
    db_path = memory_store.memory_palace_dir / "memory_palace.db"
    affected = 0
    summary_skip_types = {"session_summary", "daily_summary", "group_knowledge"}
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT memory_id, payload
                FROM memory_items
                WHERE group_id = ? AND created_at BETWEEN ? AND ?
                """,
                (str(group_id), float(since_ts), float(until_ts)),
            ).fetchall()
            for row in rows:
                mid = str(row["memory_id"] or "")
                if not mid:
                    continue
                payload = _json_loads(row["payload"], None)
                if not isinstance(payload, dict):
                    continue
                mtype = str(payload.get("memory_type", "") or "").strip().lower()
                if mtype in summary_skip_types:
                    # 不强化摘要本身（避免自增）
                    continue
                payload["reinforcement_count"] = int(payload.get("reinforcement_count", 0) or 0) + 1
                payload["tier"] = TIER_BACKGROUND
                if triggered_by:
                    refs = payload.get("summary_refs")
                    if not isinstance(refs, list):
                        refs = []
                    if triggered_by not in refs:
                        refs.append(triggered_by)
                    payload["summary_refs"] = refs[-5:]  # 限长，避免无限增长
                import json as _json

                conn.execute(
                    """
                    UPDATE memory_items
                    SET payload=?, reinforcement_count=?
                    WHERE memory_id=?
                    """,
                    (
                        _json.dumps(payload, ensure_ascii=False),
                        int(payload["reinforcement_count"]),
                        mid,
                    ),
                )
                affected += 1
            conn.commit()
    except Exception:
        return affected
    return affected


def assign_tier_on_write(payload: dict[str, Any]) -> dict[str, Any]:
    """write_memory_item 调用：若 payload 没设 tier，按当前状态分类后填入。
    不会覆盖调用者显式指定的 tier。"""
    if "tier" not in payload or not str(payload.get("tier") or "").strip():
        payload["tier"] = classify_initial_tier(payload)
    return payload


__all__ = [
    "ALL_TIERS",
    "TIER_BACKGROUND",
    "TIER_EPISODIC",
    "TIER_SEMANTIC",
    "TIER_WORKING",
    "assign_tier_on_write",
    "classify_initial_tier",
    "is_protected_tier",
    "reinforce_originals",
    "should_promote",
]
