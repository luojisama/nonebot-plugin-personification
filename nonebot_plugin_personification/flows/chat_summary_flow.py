from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from ..utils import load_chat_history, set_group_topic_summary


SUMMARY_MIN_INTERVAL = 3600
SUMMARY_MIN_NEW_MSGS = 30
SUMMARY_CONTEXT_MSGS = 40

_SUMMARY_PROMPT = """以下是一个群聊的近期对话片段，格式为「昵称: 内容」：

{messages}

请用不超过 80 字概括这段对话涉及的主要话题和群体氛围，
用陈述句，如"群里最近在聊 XXX，氛围比较 XXX"。
只输出摘要本身，不要任何前缀或解释。"""


async def maybe_update_group_summary(
    group_id: str,
    *,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    logger: Any,
    plugin_config: Any = None,
    force: bool = False,
) -> None:
    if (
        plugin_config is not None
        and not getattr(plugin_config, "personification_group_summary_enabled", True)
    ):
        return

    data = load_chat_history()
    group_data = data.get(group_id)
    if not isinstance(group_data, dict):
        return

    messages = group_data.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    now = time.time()
    last_at = float(group_data.get("topic_summary_at", 0) or 0)
    if not force and last_at and now - last_at < SUMMARY_MIN_INTERVAL:
        return

    new_msg_count = len(
        [
            msg
            for msg in messages
            if isinstance(msg, dict) and float(msg.get("time", 0) or 0) > last_at
        ]
    )
    if not force and new_msg_count < SUMMARY_MIN_NEW_MSGS:
        return

    candidates = [
        msg
        for msg in messages[-60:]
        if isinstance(msg, dict) and not msg.get("is_bot") and str(msg.get("content", "")).strip()
    ][-SUMMARY_CONTEXT_MSGS:]
    if not candidates:
        return

    formatted = "\n".join(
        f"{str(msg.get('nickname', '?'))[:20]}: {str(msg.get('content', ''))[:60]}"
        for msg in candidates
    )

    try:
        result = await call_ai_api(
            [{"role": "user", "content": _SUMMARY_PROMPT.format(messages=formatted)}]
        )
        summary = str(result or "").strip()
        if summary and len(summary) > 5:
            set_group_topic_summary(group_id, summary, now)
            logger.debug(f"[chat_summary] 群 {group_id} 话题摘要已更新: {summary[:40]}")
    except Exception as e:
        logger.warning(f"[chat_summary] 群 {group_id} 摘要生成失败: {e}")
