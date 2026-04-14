import time
from typing import Any, Dict, Optional

from .data_store import get_data_store


_STORE_NAME = "proactive_state"


def load_proactive_state() -> Dict[str, Dict[str, Any]]:
    raw = get_data_store().load_sync(_STORE_NAME)
    data = raw if isinstance(raw, dict) else {}
    for user_id, user_state in list(data.items()):
        if not isinstance(user_state, dict):
            data[user_id] = {}
            continue
        user_state.setdefault("last_date", "")
        user_state.setdefault("count", 0)
        user_state.setdefault("last_interaction", 0)
        user_state.setdefault("last_proactive_at", 0)
    return data


def save_proactive_state(data: Dict[str, Dict[str, Any]]) -> None:
    get_data_store().save_sync(_STORE_NAME, data)


def update_private_interaction_time(
    user_id: str,
    proactive_state: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    state = proactive_state if proactive_state is not None else load_proactive_state()
    user_state = state.get(user_id, {})
    user_state["last_interaction"] = time.time()
    state[user_id] = user_state
    if proactive_state is None:
        get_data_store().mutate_sync(
            _STORE_NAME,
            lambda current: {
                **(current if isinstance(current, dict) else {}),
                user_id: user_state,
            },
        )
    return state


def update_group_chat_active(
    group_id: str,
    *,
    user_id: str = "",
    topic: str = "",
    active_minutes: int = 8,
    proactive_state: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    state = proactive_state if proactive_state is not None else load_proactive_state()
    key = f"group_chat_active_{group_id}"
    current = state.get(key, {})
    if not isinstance(current, dict):
        current = {}

    now_ts = time.time()
    current["until"] = now_ts + max(1, int(active_minutes)) * 60
    current["updated_at"] = now_ts
    if user_id:
        current["last_user_id"] = str(user_id)
    if topic:
        current["topic"] = str(topic).strip()[:80]
    state[key] = current

    if proactive_state is None:
        get_data_store().mutate_sync(
            _STORE_NAME,
            lambda current_state: {
                **(current_state if isinstance(current_state, dict) else {}),
                key: current,
            },
        )
    return state
