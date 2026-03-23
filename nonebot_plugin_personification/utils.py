import time
from typing import Dict, List, Optional

from .core.data_store import get_data_store


_WHITELIST_STORE = "whitelist"
_REQUESTS_STORE = "requests"
_GROUP_CONFIG_STORE = "group_config"
_CHAT_HISTORY_STORE = "chat_history"


def load_chat_history() -> Dict[str, dict]:
    data = get_data_store().load_sync(_CHAT_HISTORY_STORE)
    return data if isinstance(data, dict) else {}


def save_chat_history(data: Dict[str, dict]):
    get_data_store().save_sync(_CHAT_HISTORY_STORE, data if isinstance(data, dict) else {})


def record_group_msg(group_id: str, nickname: str, content: str, is_bot: bool = False) -> int:
    if not content.strip():
        return 0

    count = 0

    def _mutate(current: object) -> Dict[str, dict]:
        nonlocal count
        data = current if isinstance(current, dict) else {}
        group_data = data.get(group_id)
        if not isinstance(group_data, dict):
            group_data = {"style": "", "messages": []}
            data[group_id] = group_data

        messages = group_data.get("messages")
        if not isinstance(messages, list):
            messages = []
            group_data["messages"] = messages

        messages.append(
            {
                "nickname": nickname,
                "content": content,
                "time": int(time.time()),
                "is_bot": is_bot,
            }
        )
        if len(messages) > 200:
            group_data["messages"] = messages[-200:]
        count = len(group_data["messages"])
        return data

    try:
        get_data_store().mutate_sync(_CHAT_HISTORY_STORE, _mutate)
        return count
    except Exception as e:
        print(f"Error recording group msg: {e}")
        return 0


def clear_group_msgs(group_id: str):
    def _mutate(current: object) -> Dict[str, dict]:
        data = current if isinstance(current, dict) else {}
        group_data = data.get(group_id)
        if not isinstance(group_data, dict):
            return data
        group_data["messages"] = []
        return data

    get_data_store().mutate_sync(_CHAT_HISTORY_STORE, _mutate)


def get_recent_group_msgs(group_id: str, limit: int = 200) -> List[Dict]:
    data = load_chat_history()
    group_data = data.get(group_id)
    if not isinstance(group_data, dict):
        return []
    messages = group_data.get("messages")
    if not isinstance(messages, list):
        return []
    return messages[-limit:]


def set_group_style(group_id: str, style: str):
    def _mutate(current: object) -> Dict[str, dict]:
        data = current if isinstance(current, dict) else {}
        group_data = data.get(group_id)
        if not isinstance(group_data, dict):
            group_data = {"style": "", "messages": []}
            data[group_id] = group_data
        group_data["style"] = style
        return data

    get_data_store().mutate_sync(_CHAT_HISTORY_STORE, _mutate)


def get_group_style(group_id: str) -> str:
    data = load_chat_history()
    group_data = data.get(group_id)
    if not isinstance(group_data, dict):
        return ""
    return str(group_data.get("style", "") or "")


def get_group_topic_summary(group_id: str) -> str:
    data = load_chat_history()
    group_data = data.get(group_id)
    if not isinstance(group_data, dict):
        return ""
    return str(group_data.get("topic_summary", "") or "")


def set_group_topic_summary(group_id: str, summary: str, ts: float) -> None:
    def _mutate(current: object) -> Dict[str, dict]:
        data = current if isinstance(current, dict) else {}
        group_data = data.get(group_id)
        if not isinstance(group_data, dict):
            group_data = {"style": "", "messages": []}
            data[group_id] = group_data
        group_data["topic_summary"] = summary
        group_data["topic_summary_at"] = float(ts)
        return data

    get_data_store().mutate_sync(_CHAT_HISTORY_STORE, _mutate)


def load_group_configs() -> Dict[str, dict]:
    data = get_data_store().load_sync(_GROUP_CONFIG_STORE)
    return data if isinstance(data, dict) else {}


def save_group_configs(data: Dict[str, dict]):
    get_data_store().save_sync(_GROUP_CONFIG_STORE, data if isinstance(data, dict) else {})


def get_group_config(group_id: str) -> dict:
    configs = load_group_configs()
    return configs.get(group_id, {}) if isinstance(configs.get(group_id, {}), dict) else {}


def set_group_prompt(group_id: str, prompt: Optional[str]):
    def _mutate(current: object) -> Dict[str, dict]:
        configs = current if isinstance(current, dict) else {}
        group_config = configs.get(group_id)
        if not isinstance(group_config, dict):
            group_config = {}
            configs[group_id] = group_config

        if prompt is None:
            group_config.pop("custom_prompt", None)
        else:
            group_config["custom_prompt"] = prompt
        return configs

    get_data_store().mutate_sync(_GROUP_CONFIG_STORE, _mutate)


def set_group_sticker_enabled(group_id: str, enabled: bool):
    def _mutate(current: object) -> Dict[str, dict]:
        configs = current if isinstance(current, dict) else {}
        group_config = configs.get(group_id)
        if not isinstance(group_config, dict):
            group_config = {}
            configs[group_id] = group_config
        group_config["sticker_enabled"] = enabled
        return configs

    get_data_store().mutate_sync(_GROUP_CONFIG_STORE, _mutate)


def set_group_enabled(group_id: str, enabled: bool):
    def _mutate(current: object) -> Dict[str, dict]:
        configs = current if isinstance(current, dict) else {}
        group_config = configs.get(group_id)
        if not isinstance(group_config, dict):
            group_config = {}
            configs[group_id] = group_config
        group_config["enabled"] = enabled
        return configs

    get_data_store().mutate_sync(_GROUP_CONFIG_STORE, _mutate)


def set_group_schedule_enabled(group_id: str, enabled: bool):
    def _mutate(current: object) -> Dict[str, dict]:
        configs = current if isinstance(current, dict) else {}
        group_config = configs.get(group_id)
        if not isinstance(group_config, dict):
            group_config = {}
            configs[group_id] = group_config
        group_config["schedule_enabled"] = enabled
        return configs

    get_data_store().mutate_sync(_GROUP_CONFIG_STORE, _mutate)


def load_whitelist() -> list:
    data = get_data_store().load_sync(_WHITELIST_STORE)
    return data if isinstance(data, list) else []


def save_whitelist(whitelist: list):
    get_data_store().save_sync(_WHITELIST_STORE, whitelist if isinstance(whitelist, list) else [])


def add_group_to_whitelist(group_id: str) -> bool:
    added = False

    def _mutate(current: object) -> list:
        nonlocal added
        whitelist = current if isinstance(current, list) else []
        if group_id in whitelist:
            return whitelist
        whitelist.append(group_id)
        added = True
        return whitelist

    get_data_store().mutate_sync(_WHITELIST_STORE, _mutate)
    return added


def remove_group_from_whitelist(group_id: str) -> bool:
    removed = False

    def _mutate(current: object) -> list:
        nonlocal removed
        whitelist = current if isinstance(current, list) else []
        if group_id not in whitelist:
            return whitelist
        whitelist.remove(group_id)
        removed = True
        return whitelist

    get_data_store().mutate_sync(_WHITELIST_STORE, _mutate)
    return removed


def is_group_whitelisted(group_id: str, config_whitelist: list) -> bool:
    group_config = get_group_config(group_id)
    if "enabled" in group_config:
        return bool(group_config["enabled"])
    if group_id in config_whitelist:
        return True
    return group_id in load_whitelist()


def load_requests() -> Dict[str, dict]:
    data = get_data_store().load_sync(_REQUESTS_STORE)
    return data if isinstance(data, dict) else {}


def save_requests(data: Dict[str, dict]):
    get_data_store().save_sync(_REQUESTS_STORE, data if isinstance(data, dict) else {})


def add_request(group_id: str, user_id: str, group_name: str) -> bool:
    added = False

    def _mutate(current: object) -> Dict[str, dict]:
        nonlocal added
        requests = current if isinstance(current, dict) else {}
        current_request = requests.get(group_id)
        if isinstance(current_request, dict) and current_request.get("status") == "pending":
            return requests

        now = time.time()
        requests[group_id] = {
            "group_id": group_id,
            "user_id": user_id,
            "group_name": group_name,
            "status": "pending",
            "request_time": now,
            "update_time": now,
        }
        added = True
        return requests

    get_data_store().mutate_sync(_REQUESTS_STORE, _mutate)
    return added


def update_request_status(group_id: str, status: str, operator_id: str = None) -> bool:
    updated = False

    def _mutate(current: object) -> Dict[str, dict]:
        nonlocal updated
        requests = current if isinstance(current, dict) else {}
        request = requests.get(group_id)
        if not isinstance(request, dict):
            return requests

        request["status"] = status
        request["update_time"] = time.time()
        if operator_id:
            request["operator_id"] = operator_id
        updated = True
        return requests

    get_data_store().mutate_sync(_REQUESTS_STORE, _mutate)
    return updated


def get_request_info(group_id: str) -> Optional[dict]:
    requests = load_requests()
    request = requests.get(group_id)
    return request if isinstance(request, dict) else None
