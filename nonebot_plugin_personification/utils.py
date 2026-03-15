import json
import time
import threading
from pathlib import Path
from typing import List, Dict, Optional

DATA_PATH = Path("data/personification/whitelist.json")
REQUESTS_PATH = Path("data/personification/requests.json")
GROUP_CONFIG_PATH = Path("data/personification/group_config.json")

# --- 群聊消息记录 (持久化版) ---
# 存储结构: JSON文件 {group_id: {"style": str, "messages": [{nickname, content, time, is_bot}, ...]}}
CHAT_HISTORY_PATH = Path("data/personification/chat_history.json")
_chat_history_lock = threading.Lock()
_group_config_lock = threading.Lock()
_whitelist_lock = threading.Lock()
_requests_lock = threading.Lock()


def _atomic_write_json(path: Path, data: object, *, lock: threading.Lock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".{threading.get_ident()}.tmp")
    with lock:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        if temp_path.exists():
            temp_path.replace(path)

def load_chat_history() -> Dict[str, dict]:
    """加载聊天记录"""
    if not CHAT_HISTORY_PATH.exists():
        return {}
    try:
        content = CHAT_HISTORY_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return {}
        return json.loads(content)
    except Exception as e:
        print(f"Error loading chat history: {e}") # 使用 print 临时调试，因为这里可能没有 logger
        return {}

def save_chat_history(data: Dict[str, dict]):
    """保存聊天记录"""
    try:
        _atomic_write_json(CHAT_HISTORY_PATH, data, lock=_chat_history_lock)
    except Exception as e:
        print(f"Error saving chat history: {e}")

def record_group_msg(group_id: str, nickname: str, content: str, is_bot: bool = False) -> int:
    """
    记录群聊消息
    返回当前记录的总数
    """
    # 过滤空消息
    if not content.strip():
        return 0
        
    try:
        data = load_chat_history()
        if group_id not in data:
            data[group_id] = {"style": "", "messages": []}
        
        # 确保 messages 字段存在 (兼容旧数据)
        if "messages" not in data[group_id]:
            data[group_id]["messages"] = []
            
        data[group_id]["messages"].append({
            "nickname": nickname,
            "content": content,
            "time": int(time.time()),
            "is_bot": is_bot
        })
        
        # 限制每群保留最近 200 条
        count = len(data[group_id]["messages"])
        if count > 200:
            data[group_id]["messages"] = data[group_id]["messages"][-200:]
            count = 200
            
        save_chat_history(data)
        return count
    except Exception as e:
        print(f"Error recording group msg: {e}")
        return 0

def clear_group_msgs(group_id: str):
    """清空指定群聊的历史记录"""
    data = load_chat_history()
    if group_id in data:
        data[group_id]["messages"] = []
        save_chat_history(data)

def get_recent_group_msgs(group_id: str, limit: int = 200) -> List[Dict]:
    """获取最近群聊消息"""
    data = load_chat_history()
    if group_id not in data or "messages" not in data[group_id]:
        return []
    return data[group_id]["messages"][-limit:]

def set_group_style(group_id: str, style: str):
    """设置群聊风格 (存入 chat_history.json)"""
    data = load_chat_history()
    if group_id not in data:
        data[group_id] = {"style": "", "messages": []}
        
    data[group_id]["style"] = style
    save_chat_history(data)

def get_group_style(group_id: str) -> str:
    """获取群聊风格"""
    data = load_chat_history()
    return data.get(group_id, {}).get("style", "")

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
    _atomic_write_json(GROUP_CONFIG_PATH, data, lock=_group_config_lock)

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

def set_group_schedule_enabled(group_id: str, enabled: bool):
    configs = load_group_configs()
    if group_id not in configs:
        configs[group_id] = {}
        
    configs[group_id]["schedule_enabled"] = enabled
    save_group_configs(configs)

# 旧的 set_group_style / get_group_style 已迁移至 chat_history.json 管理
# 保留这两个函数名是为了兼容接口，但实际操作 chat_history.json
# 上面的新实现已经覆盖了这两个函数，所以这里只需要删除旧的实现即可
# (由于 SearchReplace 替换了上面的部分，这里不需要重复删除，只需确保不重复定义)

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
    _atomic_write_json(DATA_PATH, whitelist, lock=_whitelist_lock)

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
    _atomic_write_json(REQUESTS_PATH, data, lock=_requests_lock)

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
