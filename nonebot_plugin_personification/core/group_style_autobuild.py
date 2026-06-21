from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from .data_store import get_data_store
from .db import connect_sync
from .group_knowledge_autobuild import _load_messages_since, _format_chat_summary

try:  # 诊断日志用；测试/无 nonebot 环境下降级为 None
    from nonebot import logger as _logger
except Exception:  # pragma: no cover
    _logger = None


_NS_LAST_RUN = "group_style_last_run"
_NS_DAILY_COUNT = "group_style_daily_count"

_DEFAULT_MIN_MESSAGES = 100
_DEFAULT_DAILY_LIMIT = 2
_DEFAULT_INTERVAL_HOURS = 12
_DEFAULT_KEEP_SNAPSHOTS = 3
_MAX_WINDOW_MESSAGES = 250


_GROUP_STYLE_PROMPT = (
    "你是群聊风格分析师。这是角色扮演陪伴机器人的内部任务，用于让 bot 融入群聊语气，"
    "属于正常产品功能；请直接完成并按格式输出，不要拒绝或回复'无法完成/不在能力范围'之类的话。"
    "下面是一段群聊对话摘要。"
    "请总结这个群当前的整体说话风格，包含 5 个维度："
    "tone（语气/氛围，10-20 字），"
    "pace（节奏，慢/中/快+特点），"
    "catchphrases（口头禅/常用感叹词列表，3-6 项），"
    "taboos（禁忌或敏感话题，0-3 项），"
    "typical_length（典型单句长度，短/中/长+说明）。"
    "只输出严格 JSON 对象，不要 markdown。"
    "\n格式：{\"tone\":\"...\",\"pace\":\"...\",\"catchphrases\":[\"...\"],\"taboos\":[\"...\"],\"typical_length\":\"...\"}"
)


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
        data = {today: data.get(today, {})} if isinstance(data.get(today), dict) else {today: {}}
        entry = data[today]
        entry[str(group_id)] = int(entry.get(str(group_id), 0) or 0) + 1
        new_count[0] = entry[str(group_id)]
        return data

    get_data_store().mutate_sync(_NS_DAILY_COUNT, _mutate)
    return new_count[0]


def _style_to_text(style: dict[str, Any]) -> str:
    """把 JSON 五维度展平成 prompt 可注入的 1 段中文。"""
    parts: list[str] = []
    if style.get("tone"):
        parts.append(f"语气：{style['tone']}")
    if style.get("pace"):
        parts.append(f"节奏：{style['pace']}")
    catchphrases = style.get("catchphrases") or []
    if isinstance(catchphrases, list) and catchphrases:
        parts.append("口头禅：" + "、".join(str(c) for c in catchphrases[:6] if c))
    taboos = style.get("taboos") or []
    if isinstance(taboos, list) and taboos:
        parts.append("禁忌：" + "、".join(str(t) for t in taboos[:3] if t))
    if style.get("typical_length"):
        parts.append(f"典型句长：{style['typical_length']}")
    return "；".join(parts)


def _save_snapshot(group_id: str, style_text: str, style_json: dict[str, Any], *, keep: int = _DEFAULT_KEEP_SNAPSHOTS) -> int:
    """写入 snapshot，并把同 group_id 超出 keep 条数的旧记录删除。返回新条目 id。"""
    payload = json.dumps(style_json or {}, ensure_ascii=False)
    now_ts = time.time()
    with connect_sync() as conn:
        cursor = conn.execute(
            """
            INSERT INTO group_style_snapshots (group_id, style_text, style_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(group_id), str(style_text or ""), payload, now_ts),
        )
        new_id = int(cursor.lastrowid or 0)
        conn.execute(
            """
            DELETE FROM group_style_snapshots
            WHERE group_id = ?
              AND id NOT IN (
                SELECT id FROM group_style_snapshots
                WHERE group_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
              )
            """,
            (str(group_id), str(group_id), max(1, int(keep))),
        )
        conn.commit()
    return new_id


def list_style_snapshots(group_id: str, limit: int = _DEFAULT_KEEP_SNAPSHOTS) -> list[dict[str, Any]]:
    with connect_sync() as conn:
        rows = conn.execute(
            """
            SELECT id, group_id, style_text, style_json, created_at
            FROM group_style_snapshots
            WHERE group_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (str(group_id), int(max(1, limit))),
        ).fetchall()
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["style_json"] or "{}")
        except Exception:
            payload = {}
        snapshots.append({
            "id": int(row["id"]),
            "group_id": str(row["group_id"]),
            "style_text": str(row["style_text"] or ""),
            "style_json": payload if isinstance(payload, dict) else {},
            "created_at": float(row["created_at"] or 0),
        })
    return snapshots


def get_latest_style_text(group_id: str) -> str:
    snapshots = list_style_snapshots(group_id, limit=1)
    if not snapshots:
        return ""
    return snapshots[0]["style_text"]


async def build_group_style(
    *,
    tool_caller: Any,
    memory_store: Any,
    group_id: str,
    chat_summary: str,
    keep_snapshots: int = _DEFAULT_KEEP_SNAPSHOTS,
) -> dict[str, Any]:
    """调 LLM 抽取群风格 5 维度 JSON，写 snapshot 并返回。"""
    if not tool_caller or not chat_summary:
        return {}
    token = None
    try:
        from .llm_context import reset_llm_context, set_llm_context

        token = set_llm_context(purpose="group_style", group_id=str(group_id or ""))
    except Exception:
        token = None
    try:
        from .safety_filter import SafetyRefusalError, sanitize_or_retry
        from .token_ledger import record_response_usage

        async def _first() -> Any:
            return await tool_caller.chat_with_tools(
                messages=[
                    {"role": "system", "content": _GROUP_STYLE_PROMPT},
                    {"role": "user", "content": str(chat_summary)[:4000]},
                ],
                tools=[],
                use_builtin_search=False,
            )

        async def _retry() -> Any:
            return await tool_caller.chat_with_tools(
                messages=[
                    {
                        "role": "system",
                        "content": _GROUP_STYLE_PROMPT
                        + "\n注意：请只输出 JSON，不要任何拒绝、'抱歉'、'作为AI'之类的开场。",
                    },
                    {"role": "user", "content": str(chat_summary)[:4000]},
                ],
                tools=[],
                use_builtin_search=False,
            )

        try:
            response = await sanitize_or_retry(
                call=_first,
                retry_call=_retry,
                on_response=record_response_usage,
                logger=_logger,
                purpose="group_style",
            )
        except SafetyRefusalError as e:
            if _logger is not None:
                if getattr(e, "source", "") == "api_block":
                    _logger.warning(
                        f"[group_style] 群 {group_id} 风格分析被供应商安全策略拦截"
                        f"（{getattr(e, 'reason', '') or '未知原因'}）：本轮跳过。"
                    )
                else:
                    _logger.info(f"[group_style] 群 {group_id} 风格分析返回拒绝模板，本轮跳过。")
            return {}
    except Exception:
        return {}
    finally:
        if token is not None:
            try:
                reset_llm_context(token)
            except Exception:
                pass
    content = str(getattr(response, "content", "") or "").strip()
    style_json: dict[str, Any] = {}
    try:
        style_json = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            try:
                style_json = json.loads(match.group(0))
            except json.JSONDecodeError:
                style_json = {}
    if not isinstance(style_json, dict) or not style_json:
        return {}
    style_text = _style_to_text(style_json)
    snapshot_id = _save_snapshot(group_id, style_text, style_json, keep=keep_snapshots)
    return {"id": snapshot_id, "style_text": style_text, "style_json": style_json}


async def scan_groups_for_style(
    *,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
    min_messages: int = _DEFAULT_MIN_MESSAGES,
    daily_limit: int = _DEFAULT_DAILY_LIMIT,
    keep_snapshots: int = _DEFAULT_KEEP_SNAPSHOTS,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"groups": []}
    if memory_store is None or tool_caller is None:
        return summary
    try:
        group_ids = list(memory_store.list_groups())
    except Exception:
        return summary
    for gid in group_ids:
        try:
            if _get_daily_count(gid) >= max(0, int(daily_limit)):
                summary["groups"].append({"group_id": gid, "status": "daily_limit"})
                continue
            since = _get_last_run(gid)
            rows = _load_messages_since(
                memory_store=memory_store,
                group_id=gid,
                since_ts=since,
                limit=_MAX_WINDOW_MESSAGES,
            )
            if len(rows) < max(1, int(min_messages)):
                summary["groups"].append({"group_id": gid, "status": "below_threshold", "messages": len(rows)})
                continue
            chat_summary = _format_chat_summary(rows)
            built = await build_group_style(
                tool_caller=tool_caller,
                memory_store=memory_store,
                group_id=gid,
                chat_summary=chat_summary,
                keep_snapshots=keep_snapshots,
            )
            _set_last_run(gid, time.time())
            if built:
                _incr_daily_count(gid)
                summary["groups"].append({"group_id": gid, "status": "built", "snapshot_id": built["id"]})
            else:
                summary["groups"].append({"group_id": gid, "status": "llm_failed"})
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[group_style_autobuild] 群 {gid} 构建失败：{exc}")
            summary["groups"].append({"group_id": gid, "status": "error", "error": str(exc)})
    return summary


def register_group_style_autobuild_job(
    *,
    scheduler: Any,
    plugin_config: Any,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
) -> None:
    if not bool(getattr(plugin_config, "personification_group_style_autobuild_enabled", True)):
        if logger is not None:
            logger.info("[group_style_autobuild] disabled via config; skip job registration")
        return
    interval_hours = max(
        1,
        int(getattr(plugin_config, "personification_group_style_interval_hours", _DEFAULT_INTERVAL_HOURS) or _DEFAULT_INTERVAL_HOURS),
    )
    min_messages = max(
        20,
        int(getattr(plugin_config, "personification_group_style_min_messages", _DEFAULT_MIN_MESSAGES) or _DEFAULT_MIN_MESSAGES),
    )
    daily_limit = max(
        1,
        int(getattr(plugin_config, "personification_group_style_daily_limit", _DEFAULT_DAILY_LIMIT) or _DEFAULT_DAILY_LIMIT),
    )

    async def _job() -> None:
        try:
            await scan_groups_for_style(
                memory_store=memory_store,
                tool_caller=tool_caller,
                logger=logger,
                min_messages=min_messages,
                daily_limit=daily_limit,
            )
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[group_style_autobuild] 扫描失败：{exc}")

    try:
        scheduler.add_job(
            _job,
            "interval",
            hours=interval_hours,
            id="personification_group_style_autobuild",
            replace_existing=True,
        )
        if logger is not None:
            logger.info(
                f"[group_style_autobuild] 已注册扫描任务：每 {interval_hours} 小时；阈值 {min_messages} 条；每群每日 {daily_limit} 次；保留 {_DEFAULT_KEEP_SNAPSHOTS} 个快照"
            )
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[group_style_autobuild] 注册失败：{exc}")


__all__ = [
    "build_group_style",
    "scan_groups_for_style",
    "register_group_style_autobuild_job",
    "list_style_snapshots",
    "get_latest_style_text",
]
