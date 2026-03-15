import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROACTIVE_STATE_PATH = Path("data/personification/proactive_state.json")


def load_proactive_state() -> Dict[str, Dict[str, Any]]:
    if PROACTIVE_STATE_PATH.exists():
        try:
            with open(PROACTIVE_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for user_id, user_state in data.items():
                        if not isinstance(user_state, dict):
                            data[user_id] = {}
                            continue
                        user_state.setdefault("last_date", "")
                        user_state.setdefault("count", 0)
                        user_state.setdefault("last_interaction", 0)
                        user_state.setdefault("last_proactive_at", 0)
                return data
        except Exception:
            return {}
    return {}


def save_proactive_state(data: Dict[str, Dict[str, Any]]) -> None:
    PROACTIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PROACTIVE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def update_private_interaction_time(
    user_id: str,
    proactive_state: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    state = proactive_state if proactive_state is not None else load_proactive_state()
    user_state = state.get(user_id, {})
    user_state["last_interaction"] = time.time()
    state[user_id] = user_state
    if proactive_state is None:
        save_proactive_state(state)
    return state
