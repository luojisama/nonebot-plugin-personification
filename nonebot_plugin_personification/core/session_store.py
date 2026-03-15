import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from nonebot import logger

SESSION_HISTORY_PATH = Path("data/personification/session_histories.json")
SESSION_HISTORY_LIMIT = 100
GROUP_SESSION_PREFIX = "group_"
PRIVATE_SESSION_PREFIX = "private_"


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
    return SESSION_HISTORY_LIMIT


def trim_session_history(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    history = ensure_session_history(session_id, legacy_session_id=legacy_session_id)
    limit = get_session_history_limit(session_id)
    if len(history) > limit:
        # Keep existing plugin behavior: clear when over limit.
        chat_histories[session_id] = []
        save_session_histories()
    return chat_histories[session_id]


def append_session_message(
    session_id: str,
    role: str,
    content: Any,
    legacy_session_id: Optional[str] = None,
    **metadata: Any,
) -> List[Dict]:
    message = {"role": role, "content": content}
    message.update({key: value for key, value in metadata.items() if value is not None})
    history = ensure_session_history(session_id, legacy_session_id=legacy_session_id)
    if len(history) >= get_session_history_limit(session_id):
        history.clear()
    history.append(message)
    save_session_histories()
    return history


def get_session_messages(session_id: str, legacy_session_id: Optional[str] = None) -> List[Dict]:
    return trim_session_history(session_id, legacy_session_id=legacy_session_id)
