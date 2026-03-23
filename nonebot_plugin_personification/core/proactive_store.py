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
