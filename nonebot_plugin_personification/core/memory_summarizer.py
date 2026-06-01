from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from .data_store import get_data_store
from .memory_store import _connect, _json_loads


_NS_SESSION_LAST_RUN = "memory_summarizer_session_last_run"
_NS_DAILY_LAST_RUN = "memory_summarizer_daily_last_run"

_SESSION_MIN_MESSAGES = 30
_SESSION_MAX_MESSAGES = 120
_SESSION_INTERVAL_SECONDS = 2 * 3600  # 2 hours
_DAILY_HOUR = 0  # midnight


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _get_last_run(namespace: str, group_id: str) -> float:
    data = get_data_store().load_sync(namespace)
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get(str(group_id), 0) or 0)
    except Exception:
        return 0.0


def _set_last_run(namespace: str, group_id: str, ts: float) -> None:
    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data[str(group_id)] = float(ts)
        return data

    get_data_store().mutate_sync(namespace, _mutate)


def _load_messages_window(
    *,
    memory_store: Any,
    group_id: str,
    since_ts: float,
    limit: int = _SESSION_MAX_MESSAGES,
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


def _format_for_summary(rows: list[dict[str, Any]], *, max_chars: int = 3000) -> str:
    lines: list[str] = []
    total = 0
    for row in rows:
        speaker = row.get("user_id") or "用户"
        snippet = (row.get("text") or "")[:160]
        if not snippet:
            continue
        line = f"{speaker}: {snippet}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


async def summarize_session_segment(
    *,
    tool_caller: Any,
    group_id: str,
    messages: list[dict[str, Any]],
) -> str | None:
    if not tool_caller or not messages:
        return None
    chat_text = _format_for_summary(messages)
    if not chat_text.strip():
        return None
    prompt = (
        "你是群聊摘要生成器。下面是一段群聊记录片段。"
        "请用 2-4 句话概括这段对话的核心内容、参与者的主要观点和情绪倾向。"
        "不要逐条复述，抓重点。只输出纯文本摘要，不要 JSON 或 markdown。"
    )
    retry_prompt = prompt + "\n注意：不要输出'抱歉''作为AI''无法回答'这类拒绝模板，只输出对话摘要。"
    token = None
    try:
        from .llm_context import reset_llm_context, set_llm_context

        token = set_llm_context(purpose="memory_summarizer", group_id=str(group_id or ""))
    except Exception:
        token = None
    try:
        from .safety_filter import SafetyRefusalError, sanitize_or_retry
        from .token_ledger import record_response_usage

        async def _first() -> Any:
            return await tool_caller.chat_with_tools(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chat_text},
                ],
                tools=[],
                use_builtin_search=False,
            )

        async def _retry() -> Any:
            return await tool_caller.chat_with_tools(
                messages=[
                    {"role": "system", "content": retry_prompt},
                    {"role": "user", "content": chat_text},
                ],
                tools=[],
                use_builtin_search=False,
            )

        try:
            response = await sanitize_or_retry(
                call=_first,
                retry_call=_retry,
                on_response=record_response_usage,
                purpose="memory_summarizer_session",
            )
        except SafetyRefusalError:
            return None
        return str(getattr(response, "content", "") or "").strip() or None
    except Exception:
        return None
    finally:
        if token is not None:
            try:
                reset_llm_context(token)
            except Exception:
                pass


async def scan_groups_for_session_summaries(
    *,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
    min_messages: int = _SESSION_MIN_MESSAGES,
) -> dict[str, Any]:
    result: dict[str, Any] = {"groups": []}
    if memory_store is None or tool_caller is None:
        return result
    try:
        group_ids = list(memory_store.list_groups())
    except Exception:
        return result

    for gid in group_ids:
        try:
            since = _get_last_run(_NS_SESSION_LAST_RUN, gid)
            now = time.time()
            if since > 0 and (now - since) < _SESSION_INTERVAL_SECONDS:
                result["groups"].append({"group_id": gid, "status": "too_recent"})
                continue
            rows = _load_messages_window(memory_store=memory_store, group_id=gid, since_ts=since)
            if len(rows) < max(1, int(min_messages)):
                result["groups"].append({"group_id": gid, "status": "below_threshold", "messages": len(rows)})
                continue
            summary_text = await summarize_session_segment(
                tool_caller=tool_caller,
                group_id=gid,
                messages=rows,
            )
            if not summary_text:
                result["groups"].append({"group_id": gid, "status": "empty_summary"})
                continue
            participants = list({str(r.get("user_id", "")) for r in rows if r.get("user_id")})
            earliest = min((r["created_at"] for r in rows if r.get("created_at")), default=now)
            memory_id = f"session_summary_{gid}_{int(now)}"
            memory_store.write_memory_item(
                {
                    "memory_id": memory_id,
                    "memory_type": "session_summary",
                    "summary": summary_text,
                    "group_id": str(gid),
                    "participants": participants[:20],
                    "time_created": earliest,
                    "confidence": 0.6,
                    "salience": 0.35,
                    "stability": 0.5,
                    "source_kind": "auto_session_summary",
                    "permission_type": "public_preference",
                    "supports_recall": True,
                    "supports_autofill": False,
                    "tier": "semantic",
                }
            )
            # P4：反向加固窗口内的原始 episodic 条目，让它们落到 background 受保护
            try:
                from .memory_tier import reinforce_originals

                reinforce_originals(
                    memory_store,
                    group_id=str(gid),
                    since_ts=float(earliest),
                    until_ts=float(now),
                    triggered_by=memory_id,
                )
            except Exception:
                pass
            _set_last_run(_NS_SESSION_LAST_RUN, gid, now)
            result["groups"].append({"group_id": gid, "status": "summarized", "messages": len(rows)})
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[memory_summarizer] 群 {gid} 会话摘要失败: {exc}")
            result["groups"].append({"group_id": gid, "status": "error", "error": str(exc)})
    return result


async def scan_groups_for_daily_summaries(
    *,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {"groups": []}
    if memory_store is None or tool_caller is None:
        return result
    today = _today_key()
    try:
        group_ids = list(memory_store.list_groups())
    except Exception:
        return result

    for gid in group_ids:
        try:
            last_daily = _get_last_run(_NS_DAILY_LAST_RUN, gid)
            now = time.time()
            if last_daily > 0 and (now - last_daily) < 20 * 3600:
                result["groups"].append({"group_id": gid, "status": "already_done_today"})
                continue
            day_start = now - 24 * 3600
            rows = _load_messages_window(
                memory_store=memory_store,
                group_id=gid,
                since_ts=day_start,
                limit=300,
            )
            if len(rows) < 10:
                result["groups"].append({"group_id": gid, "status": "too_few", "messages": len(rows)})
                continue
            chat_text = _format_for_summary(rows, max_chars=4000)
            prompt = (
                "你是群聊每日摘要生成器。下面是过去 24 小时的群聊记录。"
                "请用 3-6 句话概括今天群里聊了什么、参与者的整体氛围和关键话题。"
                "不要逐条复述，抓重点。只输出纯文本摘要。"
            )
            retry_prompt = prompt + "\n注意：不要输出'抱歉''作为AI'之类的拒绝模板，只输出群聊摘要。"
            token = None
            try:
                from .llm_context import reset_llm_context, set_llm_context

                token = set_llm_context(purpose="memory_summarizer_daily", group_id=str(gid or ""))
            except Exception:
                token = None
            try:
                from .safety_filter import SafetyRefusalError, sanitize_or_retry
                from .token_ledger import record_response_usage

                async def _first() -> Any:
                    return await tool_caller.chat_with_tools(
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": chat_text},
                        ],
                        tools=[],
                        use_builtin_search=False,
                    )

                async def _retry() -> Any:
                    return await tool_caller.chat_with_tools(
                        messages=[
                            {"role": "system", "content": retry_prompt},
                            {"role": "user", "content": chat_text},
                        ],
                        tools=[],
                        use_builtin_search=False,
                    )

                try:
                    response = await sanitize_or_retry(
                        call=_first,
                        retry_call=_retry,
                        on_response=record_response_usage,
                        purpose="memory_summarizer_daily",
                    )
                except SafetyRefusalError:
                    result["groups"].append({"group_id": gid, "status": "safety_refusal"})
                    continue
            except Exception:
                result["groups"].append({"group_id": gid, "status": "llm_error"})
                continue
            finally:
                if token is not None:
                    try:
                        reset_llm_context(token)
                    except Exception:
                        pass

            summary_text = str(getattr(response, "content", "") or "").strip()
            if not summary_text:
                result["groups"].append({"group_id": gid, "status": "empty_summary"})
                continue

            participants = list({str(r.get("user_id", "")) for r in rows if r.get("user_id")})
            memory_id = f"daily_summary_{gid}_{today}"
            memory_store.write_memory_item(
                {
                    "memory_id": memory_id,
                    "memory_type": "daily_summary",
                    "summary": summary_text,
                    "group_id": str(gid),
                    "participants": participants[:30],
                    "time_created": now - 24 * 3600,
                    "confidence": 0.65,
                    "salience": 0.4,
                    "stability": 0.6,
                    "source_kind": "auto_daily_summary",
                    "permission_type": "public_preference",
                    "supports_recall": True,
                    "supports_autofill": False,
                    "tier": "semantic",
                }
            )
            # P4：反向加固这 24h 窗口内的原始事件
            try:
                from .memory_tier import reinforce_originals

                reinforce_originals(
                    memory_store,
                    group_id=str(gid),
                    since_ts=float(now - 24 * 3600),
                    until_ts=float(now),
                    triggered_by=memory_id,
                )
            except Exception:
                pass
            _set_last_run(_NS_DAILY_LAST_RUN, gid, now)
            result["groups"].append({"group_id": gid, "status": "summarized", "messages": len(rows)})
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[memory_summarizer] 群 {gid} 每日摘要失败: {exc}")
            result["groups"].append({"group_id": gid, "status": "error", "error": str(exc)})
    return result


def register_memory_summarizer_jobs(
    *,
    scheduler: Any,
    plugin_config: Any,
    memory_store: Any,
    tool_caller: Any,
    logger: Any,
) -> None:
    enabled = bool(getattr(plugin_config, "personification_memory_summarizer_enabled", True))
    if not enabled:
        if logger is not None:
            logger.info("[memory_summarizer] disabled via config; skip job registration")
        return

    async def _session_job() -> None:
        try:
            await scan_groups_for_session_summaries(
                memory_store=memory_store,
                tool_caller=tool_caller,
                logger=logger,
            )
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[memory_summarizer] 会话摘要扫描失败: {exc}")

    async def _daily_job() -> None:
        try:
            await scan_groups_for_daily_summaries(
                memory_store=memory_store,
                tool_caller=tool_caller,
                logger=logger,
            )
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[memory_summarizer] 每日摘要失败: {exc}")

    try:
        scheduler.add_job(
            _session_job,
            "interval",
            hours=2,
            id="personification_session_summarizer",
            replace_existing=True,
        )
        if logger is not None:
            logger.info("[memory_summarizer] 已注册会话片段摘要任务：每 2 小时")
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[memory_summarizer] 注册会话摘要任务失败：{exc}")

    try:
        scheduler.add_job(
            _daily_job,
            "cron",
            hour=0,
            minute=15,
            id="personification_daily_summarizer",
            replace_existing=True,
        )
        if logger is not None:
            logger.info("[memory_summarizer] 已注册每日群摘要任务：00:15")
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[memory_summarizer] 注册每日摘要任务失败：{exc}")


__all__ = [
    "register_memory_summarizer_jobs",
    "scan_groups_for_daily_summaries",
    "scan_groups_for_session_summaries",
    "summarize_session_segment",
]
