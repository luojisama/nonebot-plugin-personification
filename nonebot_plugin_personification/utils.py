import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import nonebot_plugin_localstore as localstore

PRIMARY_DATA_DIR = localstore.get_plugin_data_dir() / "personification"
LEGACY_DATA_DIR = Path("data/personification")

DATA_PATH = PRIMARY_DATA_DIR / "whitelist.json"
REQUESTS_PATH = PRIMARY_DATA_DIR / "requests.json"
GROUP_CONFIG_PATH = PRIMARY_DATA_DIR / "group_config.json"
CHAT_HISTORY_PATH = PRIMARY_DATA_DIR / "chat_history.json"

LEGACY_DATA_PATH = LEGACY_DATA_DIR / "whitelist.json"
LEGACY_REQUESTS_PATH = LEGACY_DATA_DIR / "requests.json"
LEGACY_GROUP_CONFIG_PATH = LEGACY_DATA_DIR / "group_config.json"
LEGACY_CHAT_HISTORY_PATH = LEGACY_DATA_DIR / "chat_history.json"


def _load_json(primary_path: Path, legacy_path: Path, default):
    target = primary_path if primary_path.exists() else legacy_path
    if not target.exists():
        return default
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else default
    except Exception:
        return default


def load_chat_history() -> Dict[str, dict]:
    return _load_json(CHAT_HISTORY_PATH, LEGACY_CHAT_HISTORY_PATH, {})


def save_chat_history(data: Dict[str, dict]):
    try:
        CHAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = CHAT_HISTORY_PATH.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        if temp_path.exists():
            temp_path.replace(CHAT_HISTORY_PATH)
    except Exception:
        pass


def record_group_msg(group_id: str, nickname: str, content: str, is_bot: bool = False) -> int:
    if not content.strip():
        return 0

    try:
        data = load_chat_history()
        if group_id not in data:
            data[group_id] = {"style": "", "messages": []}
        if "messages" not in data[group_id]:
            data[group_id]["messages"] = []

        data[group_id]["messages"].append(
            {
                "nickname": nickname,
                "content": content,
                "time": int(time.time()),
                "is_bot": is_bot,
            }
        )

        count = len(data[group_id]["messages"])
        if count > 200:
            data[group_id]["messages"] = data[group_id]["messages"][-200:]
            count = 200

        save_chat_history(data)
        return count
    except Exception:
        return 0


def clear_group_msgs(group_id: str):
    data = load_chat_history()
    if group_id in data:
        data[group_id]["messages"] = []
        save_chat_history(data)


def get_recent_group_msgs(group_id: str, limit: int = 200) -> List[Dict]:
    data = load_chat_history()
    if group_id not in data or "messages" not in data[group_id]:
        return []
    return data[group_id]["messages"][-limit:]


def set_group_style(group_id: str, style: str):
    data = load_chat_history()
    if group_id not in data:
        data[group_id] = {"style": "", "messages": []}
    data[group_id]["style"] = style
    save_chat_history(data)


def get_group_style(group_id: str) -> str:
    data = load_chat_history()
    return data.get(group_id, {}).get("style", "")


def load_group_configs() -> Dict[str, dict]:
    return _load_json(GROUP_CONFIG_PATH, LEGACY_GROUP_CONFIG_PATH, {})


def save_group_configs(data: Dict[str, dict]):
    GROUP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GROUP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def get_group_config(group_id: str) -> dict:
    configs = load_group_configs()
    return configs.get(group_id, {})


def set_group_prompt(group_id: str, prompt: Optional[str]):
    configs = load_group_configs()
    if group_id not in configs:
        configs[group_id] = {}

    if prompt is None:
        if "custom_prompt" in configs[group_id]:
            del configs[group_id]["custom_prompt"]
    else:
        configs[group_id]["custom_prompt"] = prompt

    save_group_configs(configs)


def set_group_sticker_enabled(group_id: str, enabled: bool):
    configs = load_group_configs()
    if group_id not in configs:
        configs[group_id] = {}
    configs[group_id]["sticker_enabled"] = enabled
    save_group_configs(configs)


def set_group_enabled(group_id: str, enabled: bool):
    configs = load_group_configs()
    if group_id not in configs:
        configs[group_id] = {}
    configs[group_id]["enabled"] = enabled
    save_group_configs(configs)


def set_group_schedule_enabled(group_id: str, enabled: bool):
    configs = load_group_configs()
    if group_id not in configs:
        configs[group_id] = {}
    configs[group_id]["schedule_enabled"] = enabled
    save_group_configs(configs)


def load_whitelist() -> list:
    return _load_json(DATA_PATH, LEGACY_DATA_PATH, [])


def save_whitelist(whitelist: list):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(whitelist, f, ensure_ascii=False, indent=4)


def add_group_to_whitelist(group_id: str) -> bool:
    whitelist = load_whitelist()
    if group_id in whitelist:
        return False
    whitelist.append(group_id)
    save_whitelist(whitelist)
    return True


def remove_group_from_whitelist(group_id: str) -> bool:
    whitelist = load_whitelist()
    if group_id not in whitelist:
        return False
    whitelist.remove(group_id)
    save_whitelist(whitelist)
    return True


def is_group_whitelisted(group_id: str, config_whitelist: list) -> bool:
    group_config = get_group_config(group_id)
    if "enabled" in group_config:
        return group_config["enabled"]
    if group_id in config_whitelist:
        return True
    return group_id in load_whitelist()


def load_requests() -> Dict[str, dict]:
    return _load_json(REQUESTS_PATH, LEGACY_REQUESTS_PATH, {})


def save_requests(data: Dict[str, dict]):
    REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REQUESTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def add_request(group_id: str, user_id: str, group_name: str) -> bool:
    requests = load_requests()
    if group_id in requests and requests[group_id].get("status") == "pending":
        return False

    requests[group_id] = {
        "group_id": group_id,
        "user_id": user_id,
        "group_name": group_name,
        "status": "pending",
        "request_time": time.time(),
        "update_time": time.time(),
    }
    save_requests(requests)
    return True


def update_request_status(group_id: str, status: str, operator_id: str = None) -> bool:
    requests = load_requests()
    if group_id not in requests:
        return False

    requests[group_id]["status"] = status
    requests[group_id]["update_time"] = time.time()
    if operator_id:
        requests[group_id]["operator_id"] = operator_id

    save_requests(requests)
    return True


def get_request_info(group_id: str) -> Optional[dict]:
    requests = load_requests()
    return requests.get(group_id)
