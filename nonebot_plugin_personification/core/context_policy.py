import re
from typing import Any, Dict, Iterable, List, Set

_HISTORY_MARKER_PATTERNS = (
    r"\[发送了一张图片:[^\]]*\]",
    r"\[发送了一张图片\]",
    r"\[发送了一张表情包:[^\]]*\]",
    r"\[发送了表情包[^\]]*\]",
)
_PRIVATE_COMMAND_PREFIXES = ("/", "!", "！", "#", "＃", ".", "。")
_PRIVATE_COMMAND_KEYWORDS: Set[str] = set()


def clear_private_command_keywords() -> None:
    _PRIVATE_COMMAND_KEYWORDS.clear()


def register_private_command_keywords(command: str, aliases: Iterable[str] | None = None) -> None:
    main = (command or "").strip()
    if main:
        _PRIVATE_COMMAND_KEYWORDS.add(main)
    if not aliases:
        return
    for alias in aliases:
        cleaned = (alias or "").strip()
        if cleaned:
            _PRIVATE_COMMAND_KEYWORDS.add(cleaned)


def get_private_command_keywords() -> Set[str]:
    return set(_PRIVATE_COMMAND_KEYWORDS)


def sanitize_history_text(text: Any) -> str:
    cleaned = str(text or "")
    for pattern in _HISTORY_MARKER_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def sanitize_message_content(content: Any) -> Any:
    if isinstance(content, list):
        sanitized_items: List[Dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                sanitized_items.append({"type": "text", "text": sanitize_history_text(item.get("text", ""))})
            elif item_type == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                if str(url).startswith("data:"):
                    sanitized_items.append({"type": "text", "text": "[图片]"})
                else:
                    sanitized_items.append(item)
        return sanitized_items
    return sanitize_history_text(content)


def sanitize_session_messages(messages: List[Dict]) -> List[Dict]:
    sanitized: List[Dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        cloned = dict(msg)
        cloned["content"] = sanitize_message_content(msg.get("content", ""))
        sanitized.append(cloned)
    return sanitized


def looks_like_private_command(text: str) -> bool:
    plain = (text or "").strip()
    if not plain:
        return False
    # 私聊命令只认显式前缀、CQ 指令或已注册白名单。
    # 不再把普通短英文（hi/ok/yo）当作命令，避免吞掉自然聊天。
    if any(plain.startswith(prefix) for prefix in _PRIVATE_COMMAND_PREFIXES):
        return True
    if plain.startswith("[CQ:") or plain.startswith("CQ:"):
        return True
    if plain in _PRIVATE_COMMAND_KEYWORDS:
        return True
    return False


def _normalized_topic_key(text: Any) -> str:
    normalized = sanitize_history_text(text).lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:80]


def _token_similarity(left: str, right: str) -> float:
    left_tokens = [token for token in re.split(r"\s+", sanitize_history_text(left).lower()) if token]
    right_tokens = [token for token in re.split(r"\s+", sanitize_history_text(right).lower()) if token]
    if not left_tokens or not right_tokens:
        return 0.0
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    overlap = len(left_set & right_set)
    total = max(len(left_set), len(right_set), 1)
    return overlap / total


def build_private_anti_loop_hint(history: List[Dict]) -> str:
    if not history:
        return ""
    recent = history[-16:]
    user_keys = [_normalized_topic_key(msg.get("content", "")) for msg in recent if msg.get("role") == "user"]
    assistant_keys = [_normalized_topic_key(msg.get("content", "")) for msg in recent if msg.get("role") == "assistant"]
    user_keys = [key for key in user_keys if key]
    assistant_keys = [key for key in assistant_keys if key]
    if not user_keys:
        return ""

    latest_user = user_keys[-1]
    repeated_user_topic = latest_user and sum(1 for key in user_keys[-3:-1] if key == latest_user) >= 1
    repeated_assistant_topic = len(assistant_keys) >= 2 and assistant_keys[-1] == assistant_keys[-2]
    assistant_texts = [sanitize_history_text(msg.get("content", "")) for msg in recent if msg.get("role") == "assistant"]
    repetitive_assistant_similarity = False
    if len(assistant_texts) >= 3:
        sims = [
            _token_similarity(assistant_texts[-1], assistant_texts[-2]),
            _token_similarity(assistant_texts[-2], assistant_texts[-3]),
        ]
        repetitive_assistant_similarity = all(sim >= 0.6 for sim in sims)
    if not repeated_user_topic and not repeated_assistant_topic and not repetitive_assistant_similarity:
        return ""

    hint = (
        "## Anti-loop guard (high priority)\n"
        "- The latest turns are becoming repetitive around one topic.\n"
        "- Prioritize the newest user input and avoid repeating old points.\n"
        "- If there is no new information, reply in <= 12 Chinese characters or output [SILENCE].\n"
    )
    if repetitive_assistant_similarity:
        hint += "- 当前回复已出现明显重复，你必须换一个完全不同的角度或话题切入。\n"
    return hint


def stringify_history_content(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                parts.append(sanitize_history_text(item.get("text", "")))
            elif item_type == "image_url":
                parts.append("[图片]")
        return "".join(parts).strip()
    return sanitize_history_text(content)
