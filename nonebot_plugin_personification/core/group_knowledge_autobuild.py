from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from .data_store import get_data_store
from .group_knowledge import build_group_knowledge
from .memory_store import _connect, _json_loads


_NS_LAST_RUN = "group_knowledge_last_run"
_NS_DAILY_COUNT = "group_knowledge_daily_count"

_DEFAULT_MIN_MESSAGES = 50
_DEFAULT_DAILY_LIMIT = 6
_DEFAULT_INTERVAL_HOURS = 4
_MAX_WINDOW_MESSAGES = 200


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _get_last_run(group_id: str) -> float:
    data = get_data_store().load_sync(_NS_LAST_RUN)
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get(str(group_id), 0) or 0)
    except Exception:
        return 0.0


def _set_last_run(group_id: str, ts: float) -> None:
    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data[str(group_id)] = float(ts)
        return data

    get_data_store().mutate_sync(_NS_LAST_RUN, _mutate)


def _get_daily_count(group_id: str) -> int:
    today = _today_key()
    data = get_data_store().load_sync(_NS_DAILY_COUNT)
    if not isinstance(data, dict):
        return 0
    entry = data.get(today, {})
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get(str(group_id), 0) or 0)
    except Exception:
        return 0


def _incr_daily_count(group_id: str) -> int:
    today = _today_key()
    new_count = [0]

    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        # 仅保留今日，过期 bucket 顺手清掉
        data = {today: data.get(today, {})} if isinstance(data.get(today), dict) else {today: {}}
        entry = data[today]
        entry[str(group_id)] = int(entry.get(str(group_id), 0) or 0) + 1
        new_count[0] = entry[str(group_id)]
        return data

    get_data_store().mutate_sync(_NS_DAILY_COUNT, _mutate)
    return new_count[0]


def _load_messages_since(
    *,
    memory_store: Any,
    group_id: str,
    since_ts: float,
    limit: int = _MAX_WINDOW_MESSAGES,
) -> list[dict[str, Any]]:
    group_dir = memory_store.ensure_group_space(group_id)
    rows: list[dict[str, Any]] = []
    with _connect(group_dir / "chat_history.db") as conn:
        result = conn.execute(
            """
            SELECT content, metadata, created_at
            FROM messages
            WHERE created_at > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (float(since_ts or 0), int(limit)),
        ).fetchall()
        for row in result:
            content = _json_loads(row["content"], row["content"])
            metadata = _json_loads(row["metadata"], {})
            text = ""
            if isinstance(content, list):
                parts = [str(p.get("text", "")).strip() for p in content if isinstance(p, dict)]
                text = " ".join(part for part in parts if part)
            elif isinstance(content, dict):
                text = str(content.get("text", "") or content.get("content", "")).strip()
            else:
                text = str(content or "").strip()
            if not text:
                continue
            rows.append(
                {
                    "text": text,
                    "user_id": str(metadata.get("user_id", "") or ""),
                    "created_at": float(row["created_at"] or 0),
                }
            )
    return rows


def _format_chat_summary(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        speaker = row.get("user_id") or "用户"
        snippet = (row.get("text") or "")[:160]
        if snippet:
            lines.append(f"{speaker}: {snippet}")
    return "\n".join(lines)


async def scan_groups_for_knowledge(
    *,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
    min_messages: int = _DEFAULT_MIN_MESSAGES,
    daily_limit: int = _DEFAULT_DAILY_LIMIT,
) -> dict[str, Any]:
    """对所有已建立空间的群扫描并构建知识。返回各群结果摘要。"""
    summary: dict[str, Any] = {"groups": []}
    if memory_store is None or tool_caller is None:
        return summary
    try:
        group_ids = list(memory_store.list_groups())
    except Exception:
        return summary
    for gid in group_ids:
        try:
            current_count = _get_daily_count(gid)
            if current_count >= max(0, int(daily_limit)):
                summary["groups"].append({"group_id": gid, "status": "daily_limit", "saved": 0})
                continue
            since = _get_last_run(gid)
            rows = _load_messages_since(memory_store=memory_store, group_id=gid, since_ts=since)
            if len(rows) < max(1, int(min_messages)):
                summary["groups"].append({"group_id": gid, "status": "below_threshold", "messages": len(rows), "saved": 0})
                continue
            chat_summary = _format_chat_summary(rows)
            saved = await build_group_knowledge(
                tool_caller=tool_caller,
                memory_store=memory_store,
                group_id=gid,
                chat_summary=chat_summary,
            )
            _set_last_run(gid, time.time())
            if saved > 0:
                _incr_daily_count(gid)
            summary["groups"].append(
                {"group_id": gid, "status": "built", "messages": len(rows), "saved": int(saved)}
            )
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[group_knowledge_autobuild] 群 {gid} 构建失败: {exc}")
            summary["groups"].append({"group_id": gid, "status": "error", "error": str(exc)})
    return summary


def register_group_knowledge_autobuild_job(
    *,
    scheduler: Any,
    plugin_config: Any,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
) -> None:
    if not bool(getattr(plugin_config, "personification_group_knowledge_autobuild_enabled", True)):
        if logger is not None:
            logger.info("[group_knowledge_autobuild] disabled via config; skip job registration")
        return
    interval_hours = max(
        1,
        int(getattr(plugin_config, "personification_group_knowledge_interval_hours", _DEFAULT_INTERVAL_HOURS) or _DEFAULT_INTERVAL_HOURS),
    )
    min_messages = max(
        10,
        int(getattr(plugin_config, "personification_group_knowledge_min_messages", _DEFAULT_MIN_MESSAGES) or _DEFAULT_MIN_MESSAGES),
    )
    daily_limit = max(
        1,
        int(getattr(plugin_config, "personification_group_knowledge_daily_limit", _DEFAULT_DAILY_LIMIT) or _DEFAULT_DAILY_LIMIT),
    )

    async def _job() -> None:
        try:
            await scan_groups_for_knowledge(
                memory_store=memory_store,
                tool_caller=tool_caller,
                logger=logger,
                min_messages=min_messages,
                daily_limit=daily_limit,
            )
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[group_knowledge_autobuild] 扫描失败: {exc}")

    try:
        scheduler.add_job(
            _job,
            "interval",
            hours=interval_hours,
            id="personification_group_knowledge_autobuild",
            replace_existing=True,
        )
        if logger is not None:
            logger.info(
                f"[group_knowledge_autobuild] 已注册扫描任务：每 {interval_hours} 小时；阈值 {min_messages} 条；每群每日 {daily_limit} 次"
            )
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[group_knowledge_autobuild] 注册失败：{exc}")


def register_propose_group_knowledge_tool(*, registry: Any, memory_store: Any, logger: Any = None) -> None:
    """注册 agent 工具：让 LLM 在对话中主动声明"群内学到了新知识"。"""
    if registry is None or memory_store is None:
        return

    async def _handler(term: str = "", definition: str = "", group_id: str = "", **_kwargs) -> str:
        term_clean = str(term or "").strip()
        def_clean = str(definition or "").strip()
        gid = str(group_id or "").strip()
        if not term_clean or len(term_clean) < 2:
            return "[propose_group_knowledge] term 必须至少 2 个字符"
        if not def_clean:
            return "[propose_group_knowledge] definition 不能为空"
        if not gid:
            return "[propose_group_knowledge] 需要 group_id 才能写入对应群知识"
        memory_id = f"gk_proposed_{gid}_{term_clean[:32]}"
        try:
            memory_store.write_memory_item({
                "memory_id": memory_id,
                "memory_type": "group_knowledge",
                "summary": f"{term_clean}: {def_clean}",
                "term": term_clean,
                "definition": def_clean,
                "group_id": gid,
                "confidence": 0.8,
                "salience": 0.6,
                "source_kind": "agent_proposed",
            })
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[propose_group_knowledge] 写入失败 group={gid} term={term_clean}: {exc}")
            return f"[propose_group_knowledge] 写入失败：{exc}"
        return f"已将 {term_clean} 纳入群 {gid} 的知识库（{def_clean}）"

    try:
        from ..agent.tool_registry import AgentTool

        registry.register(
            AgentTool(
                name="propose_group_knowledge",
                description="把刚刚学到的群内常用词、绰号或内部梗写入该群的知识库，供后续对话引用",
                parameters={
                    "type": "object",
                    "properties": {
                        "term": {"type": "string", "description": "群内常用词、绰号或内部梗（≥2 字）"},
                        "definition": {"type": "string", "description": "20 字内的一句话解释"},
                        "group_id": {"type": "string", "description": "群号"},
                    },
                    "required": ["term", "definition", "group_id"],
                },
                handler=_handler,
                local=True,
                enabled=lambda: True,
                metadata={"category": "knowledge"},
            )
        )
        if logger is not None:
            logger.info("[propose_group_knowledge] 已注册 agent 工具")
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[propose_group_knowledge] 注册失败：{exc}")


__all__ = [
    "scan_groups_for_knowledge",
    "register_group_knowledge_autobuild_job",
    "register_propose_group_knowledge_tool",
]
