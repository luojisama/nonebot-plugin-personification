"""
session_store.py  会话历史存储，支持滚动窗口 + LLM 精炼压缩。

压缩流程：
  当 append_session_message 写入后历史条数 >= compress_threshold 时：
    1. 取 history[:-keep_recent] 作为待压缩片段
    2. 调用 LLM 生成一条摘要
    3. 用 [{"role":"system","content":"<摘要>"}] + history[-keep_recent:] 替换整条历史
    4. 持久化

压缩是异步后台任务（asyncio.create_task），不阻塞当前消息发送。
压缩期间同一 session 只允许一个压缩任务在跑（通过 _compressing_sessions set 去重）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from nonebot import logger

from .context_policy import sanitize_message_content, stringify_history_content

SESSION_HISTORY_PATH = Path("data/personification/session_histories.json")
SESSION_HISTORY_LIMIT = 100
GROUP_SESSION_PREFIX = "group_"
PRIVATE_SESSION_PREFIX = "private_"

# 运行时注入：由 service_factory / plugin_runtime 在启动后写入
_plugin_config: Any = None
_compress_tool_caller: Any = None

# 正在压缩的 session_id 集合，防止并发重入
_compressing_sessions: Set[str] = set()


def init_session_store(plugin_config: Any, compress_tool_caller: Any = None) -> None:
    """在插件启动时注入配置和压缩用 ToolCaller。"""
    global _plugin_config, _compress_tool_caller
    _plugin_config = plugin_config
    _compress_tool_caller = compress_tool_caller


def _get_compress_threshold() -> int:
    if _plugin_config is not None:
        return int(getattr(_plugin_config, "personification_compress_threshold", 100))
    return 100


def _get_keep_recent() -> int:
    if _plugin_config is not None:
        return int(getattr(_plugin_config, "personification_compress_keep_recent", 20))
    return 20


def load_session_histories() -> Dict[str, List[Dict]]:
    if not SESSION_HISTORY_PATH.exists():
        return {}

    try:
        raw_data = json.loads(SESSION_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"拟人插件：加载会话历史失败，将使用空历史。错误: {e}")
        return {}

    if not isinstance(raw_data, dict):
        return {}

    normalized: Dict[str, List[Dict]] = {}
    for session_id, history in raw_data.items():
        if not isinstance(session_id, str):
            continue
        if isinstance(history, list):
            normalized[session_id] = [msg for msg in history if isinstance(msg, dict)]
    return normalized


chat_histories: Dict[str, List[Dict]] = load_session_histories()


def save_session_histories() -> None:
    try:
        SESSION_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_HISTORY_PATH.write_text(
            json.dumps(chat_histories, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"拟人插件：保存会话历史失败: {e}")


def build_group_session_id(group_id: str) -> str:
    return f"{GROUP_SESSION_PREFIX}{group_id}"


def build_private_session_id(user_id: str) -> str:
    return f"{PRIVATE_SESSION_PREFIX}{user_id}"


def is_private_session_id(session_id: str) -> bool:
    return session_id.startswith(PRIVATE_SESSION_PREFIX)


def ensure_session_history(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    if session_id not in chat_histories:
        if legacy_session_id and legacy_session_id in chat_histories:
            chat_histories[session_id] = chat_histories.pop(legacy_session_id)
            save_session_histories()
        else:
            chat_histories[session_id] = []
            save_session_histories()
    return chat_histories[session_id]


def get_session_history_limit(session_id: str) -> int:
    """向后兼容接口，返回压缩阈值。"""
    return _get_compress_threshold()


def trim_session_history(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    """向后兼容接口：不再全清，直接返回当前历史。"""
    return ensure_session_history(session_id, legacy_session_id=legacy_session_id)


def get_session_messages(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    return trim_session_history(session_id, legacy_session_id=legacy_session_id)


def _build_compress_prompt(messages: List[Dict]) -> str:
    """将消息列表序列化为压缩用 prompt。"""
    lines: List[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        lines.append(f"[{role}]: {stringify_history_content(content)[:300]}")
    conversation = "\n".join(lines)
    return (
        "以下是一段对话历史，请用简洁的中文将其压缩为一段背景摘要（150字以内）。"
        "保留关键事件、用户偏好、情感变化等重要信息，忽略寒暄和无意义内容。"
        "只输出摘要文本，不要任何前缀或解释。\n\n"
        f"{conversation}"
    )


async def _run_compress(session_id: str) -> None:
    """后台任务：压缩指定 session 的历史。"""
    if session_id in _compressing_sessions:
        return
    _compressing_sessions.add(session_id)
    try:
        history = chat_histories.get(session_id)
        if not history:
            return

        threshold = _get_compress_threshold()
        keep = _get_keep_recent()
        if len(history) < threshold:
            return

        to_compress = history[:-keep] if keep > 0 else history
        recent = history[-keep:] if keep > 0 else []
        if not to_compress:
            return

        summary_text = ""
        caller = _compress_tool_caller
        if caller is not None:
            try:
                prompt = _build_compress_prompt(to_compress)
                response = await caller.chat_with_tools(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    use_builtin_search=False,
                )
                summary_text = (response.content or "").strip()
            except Exception as e:
                logger.warning(f"[session_store] compress LLM call failed for {session_id}: {e}")

        if not summary_text:
            logger.info(f"[session_store] compress fallback (no LLM) for {session_id}")
            summary_text = f"（此前 {len(to_compress)} 条历史已省略）"

        summary_msg: Dict[str, Any] = {
            "role": "system",
            "content": f"【对话历史摘要】{summary_text}",
        }
        chat_histories[session_id] = [summary_msg] + list(recent)
        save_session_histories()
        logger.info(
            f"[session_store] compressed {session_id}: "
            f"{len(to_compress)} msgs -> summary + {len(recent)} recent"
        )
    except Exception as e:
        logger.warning(f"[session_store] compress error for {session_id}: {e}")
    finally:
        _compressing_sessions.discard(session_id)


def append_session_message(
    session_id: str,
    role: str,
    content: Any,
    legacy_session_id: Optional[str] = None,
    **metadata: Any,
) -> List[Dict]:
    sanitized_content = sanitize_message_content(content)
    message = {"role": role, "content": sanitized_content}
    message.update({key: value for key, value in metadata.items() if value is not None})
    history = ensure_session_history(session_id, legacy_session_id=legacy_session_id)
    history.append(message)
    save_session_histories()

    threshold = _get_compress_threshold()
    if len(history) >= threshold and session_id not in _compressing_sessions:
        try:
            asyncio.create_task(_run_compress(session_id))
        except RuntimeError:
            pass

    return history
