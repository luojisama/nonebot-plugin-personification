import json
import time
from pathlib import Path
from typing import List, Dict, Optional
import nonebot_plugin_localstore as localstore

data_dir = localstore.get_plugin_data_dir()
DATA_PATH = data_dir / "whitelist.json"
REQUESTS_PATH = data_dir / "requests.json"
GROUP_CONFIG_PATH = data_dir / "group_config.json"

# --- 群聊配置管理 ---

def load_group_configs() -> Dict[str, dict]:
    """加载所有群组配置"""
    if not GROUP_CONFIG_PATH.exists():
        return {}
    try:
        with open(GROUP_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

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
        # 重置
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

# --- 白名单管理 ---

def load_whitelist() -> list:
    if not DATA_PATH.exists():
        return []
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

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
    # 0. 优先检查群组独立开关配置
    group_config = get_group_config(group_id)
    if "enabled" in group_config:
        return group_config["enabled"]

    # 1. 检查白名单
    if group_id in config_whitelist:
        return True
    return group_id in load_whitelist()

# --- 申请记录管理 ---

def load_requests() -> Dict[str, dict]:
    """加载所有申请记录，Key 为 group_id"""
    if not REQUESTS_PATH.exists():
        return {}
    try:
        with open(REQUESTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_requests(data: Dict[str, dict]):
    REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REQUESTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def add_request(group_id: str, user_id: str, group_name: str) -> bool:
    """添加新的申请，如果已有 pending 申请则返回 False"""
    requests = load_requests()
    
    # 检查是否已有待处理的申请
    if group_id in requests:
        current_request = requests[group_id]
        if current_request.get("status") == "pending":
            return False
            
    # 创建新申请
    requests[group_id] = {
        "group_id": group_id,
        "user_id": user_id,
        "group_name": group_name,
        "status": "pending",
        "request_time": time.time(),
        "update_time": time.time()
    }
    save_requests(requests)
    return True

def update_request_status(group_id: str, status: str, operator_id: str = None) -> bool:
    """更新申请状态 (approved/rejected)"""
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
