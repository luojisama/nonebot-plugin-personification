import json
import time
from typing import Any, Dict, List, Optional

from .core.data_store import get_data_store
from .core.db import connect_sync


_WHITELIST_STORE = "whitelist"
_REQUESTS_STORE = "requests"
_GROUP_CONFIG_STORE = "group_config"
_CHAT_HISTORY_STORE = "chat_history"

_plugin_config: Any = None


def init_utils_config(plugin_config: Any) -> None:
    global _plugin_config
    _plugin_config = plugin_config


def _get_message_expire_hours() -> float:
    if _plugin_config is not None:
        value = getattr(_plugin_config, "personification_message_expire_hours", 24.0)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 24.0
    return 24.0


def _get_group_summary_expire_hours() -> float:
    if _plugin_config is not None:
        value = getattr(_plugin_config, "personification_group_summary_expire_hours", 4.0)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 4.0
    return 4.0


def _get_group_style_auto_analyze_threshold() -> int:
    if _plugin_config is not None:
        value = getattr(_plugin_config, "personification_group_style_auto_analyze_threshold", 200)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 200
    return 200


def _get_group_style_auto_analyze_min_new_messages() -> int:
    if _plugin_config is not None:
        value = getattr(_plugin_config, "personification_group_style_auto_analyze_min_new_messages", 50)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 50
    return 50


def _get_group_style_auto_analyze_cooldown_hours() -> float:
    if _plugin_config is not None:
        value = getattr(_plugin_config, "personification_group_style_auto_analyze_cooldown_hours", 12.0)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 12.0
    return 12.0


def _load_chat_meta() -> Dict[str, dict]:
    data = get_data_store().load_sync(_CHAT_HISTORY_STORE)
    return data if isinstance(data, dict) else {}


def _save_chat_meta(data: Dict[str, dict]) -> None:
    get_data_store().save_sync(_CHAT_HISTORY_STORE, data if isinstance(data, dict) else {})


def load_chat_history() -> Dict[str, dict]:
    data = _load_chat_meta()
    for group_id in list(data.keys()):
        group_data = data.get(group_id)
        if not isinstance(group_data, dict):
            data[group_id] = {"style": "", "messages": []}
            continue
        group_data["messages"] = get_recent_group_msgs(group_id, limit=200, expire_hours=0)
    return data


def save_chat_history(data: Dict[str, dict]):
    metadata: Dict[str, dict] = {}
    for group_id, group_data in (data or {}).items():
        if not isinstance(group_data, dict):
            continue
        metadata[str(group_id)] = {k: v for k, v in group_data.items() if k != "messages"}
    _save_chat_meta(metadata)


def _upsert_chat_meta(group_id: str, mutator: Any) -> Dict[str, dict]:
    def _mutate(current: object) -> Dict[str, dict]:
        data = current if isinstance(current, dict) else {}
        group_data = data.get(group_id)
        if not isinstance(group_data, dict):
            group_data = {"style": ""}
            data[group_id] = group_data
        mutator(group_data)
        return data

    updated = get_data_store().mutate_sync(_CHAT_HISTORY_STORE, _mutate)
    return updated if isinstance(updated, dict) else {}


def record_group_msg(
    group_id: str,
    nickname: str,
    content: str,
    is_bot: bool = False,
    **metadata: Any,
) -> int:
    if not content.strip():
        return 0

    now_ts = float(metadata.pop("time", time.time()) or time.time())
    safe_metadata = {key: value for key, value in metadata.items() if value is not None}
    mentioned_ids = safe_metadata.get("mentioned_ids", [])
    if not isinstance(mentioned_ids, list):
        mentioned_ids = []

    with connect_sync() as conn:
        conn.execute(
            """
            INSERT INTO group_messages(
                group_id, user_id, nickname, content, is_bot, reply_to_msg_id, reply_to_user_id,
                mentioned_ids, is_at_bot, message_id, source_kind, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(group_id),
                str(safe_metadata.get("user_id", "") or ""),
                str(nickname or ""),
                str(content or ""),
                1 if is_bot else 0,
                safe_metadata.get("reply_to_msg_id"),
                safe_metadata.get("reply_to_user_id"),
                json.dumps(mentioned_ids, ensure_ascii=False),
                1 if bool(safe_metadata.get("is_at_bot")) else 0,
                str(safe_metadata.get("message_id", "") or "") or None,
                str(safe_metadata.get("source_kind", "bot" if is_bot else "user") or "user"),
                now_ts,
            ),
        )
        row = conn.execute(
            "SELECT COUNT(1) AS cnt FROM group_messages WHERE group_id=?",
            (str(group_id),),
        ).fetchone()
        conn.commit()
        count = int(row["cnt"] if hasattr(row, "__getitem__") else row[0]) if row else 0

    def _mutator(group_data: dict[str, Any]) -> None:
        group_data["message_total_count"] = count

    _upsert_chat_meta(str(group_id), _mutator)
    return count


def should_trigger_group_style_analysis(group_id: str, total_message_count: int) -> bool:
    threshold = _get_group_style_auto_analyze_threshold()
    if total_message_count < threshold:
        return False

    now_ts = time.time()
    cooldown_seconds = _get_group_style_auto_analyze_cooldown_hours() * 3600
    min_new_messages = _get_group_style_auto_analyze_min_new_messages()
    triggered = False

    def _mutator(group_data: dict[str, Any]) -> None:
        nonlocal triggered
        last_at = float(group_data.get("last_auto_analyze_at", 0) or 0)
        last_count = int(group_data.get("last_auto_analyze_message_count", 0) or 0)
        should_trigger = False
        if last_at <= 0 or last_count <= 0:
            should_trigger = True
        elif total_message_count - last_count >= min_new_messages and now_ts - last_at >= cooldown_seconds:
            should_trigger = True
        if should_trigger:
            group_data["last_auto_analyze_at"] = now_ts
            group_data["last_auto_analyze_message_count"] = int(total_message_count)
            triggered = True

    _upsert_chat_meta(group_id, _mutator)
    return triggered


def clear_group_msgs(group_id: str):
    with connect_sync() as conn:
        conn.execute("DELETE FROM group_messages WHERE group_id=?", (str(group_id),))
        conn.commit()


def get_group_msg_by_message_id(group_id: str, message_id: str) -> Optional[Dict[str, Any]]:
    normalized_group_id = str(group_id)
    normalized_message_id = str(message_id or "").strip()
    if not normalized_group_id or not normalized_message_id:
        return None

    with connect_sync() as conn:
        row = conn.execute(
            """
            SELECT group_id, user_id, nickname, content, is_bot, reply_to_msg_id, reply_to_user_id,
                   mentioned_ids, is_at_bot, message_id, source_kind, timestamp
            FROM group_messages
            WHERE group_id=? AND message_id=?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (normalized_group_id, normalized_message_id),
        ).fetchone()
    if not row:
        return None

    raw_mentions = row["mentioned_ids"] if hasattr(row, "__getitem__") else row[7]
    try:
        mentioned_ids = json.loads(raw_mentions) if raw_mentions else []
    except Exception:
        mentioned_ids = []
    return {
        "group_id": row["group_id"],
        "user_id": row["user_id"],
        "nickname": row["nickname"],
        "content": row["content"],
        "time": float(row["timestamp"] or 0),
        "is_bot": bool(row["is_bot"]),
        "reply_to_msg_id": row["reply_to_msg_id"],
        "reply_to_user_id": row["reply_to_user_id"],
        "mentioned_ids": mentioned_ids if isinstance(mentioned_ids, list) else [],
        "is_at_bot": bool(row["is_at_bot"]),
        "message_id": row["message_id"],
        "source_kind": row["source_kind"],
    }


def get_recent_group_msgs(group_id: str, limit: int = 200, expire_hours: Optional[float] = None) -> List[Dict]:
    if expire_hours is None:
        expire_hours = _get_message_expire_hours()

    clauses = ["group_id=?"]
    params: list[Any] = [str(group_id)]
    if expire_hours > 0:
        clauses.append("timestamp>=?")
        params.append(time.time() - expire_hours * 3600)
    params.append(max(1, int(limit)))

    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT group_id, user_id, nickname, content, is_bot, reply_to_msg_id, reply_to_user_id,
                   mentioned_ids, is_at_bot, message_id, source_kind, timestamp
            FROM group_messages
            WHERE {" AND ".join(clauses)}
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    messages: List[Dict[str, Any]] = []
    for row in reversed(rows):
        raw_mentions = row["mentioned_ids"] if hasattr(row, "__getitem__") else row[7]
        try:
            mentioned_ids = json.loads(raw_mentions) if raw_mentions else []
        except Exception:
            mentioned_ids = []
        messages.append(
            {
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "nickname": row["nickname"],
                "content": row["content"],
                "time": float(row["timestamp"] or 0),
                "is_bot": bool(row["is_bot"]),
                "reply_to_msg_id": row["reply_to_msg_id"],
                "reply_to_user_id": row["reply_to_user_id"],
                "mentioned_ids": mentioned_ids if isinstance(mentioned_ids, list) else [],
                "is_at_bot": bool(row["is_at_bot"]),
                "message_id": row["message_id"],
                "source_kind": row["source_kind"],
            }
        )
    return messages


def build_group_context_window(
    group_id: str,
    *,
    limit: int = 6,
    include_message_ids: Optional[list[str]] = None,
) -> List[Dict[str, Any]]:
    messages = list(get_recent_group_msgs(group_id, limit=limit))
    seen_message_ids = {
        str(msg.get("message_id", "") or "").strip()
        for msg in messages
        if isinstance(msg, dict) and str(msg.get("message_id", "") or "").strip()
    }
    extras: List[Dict[str, Any]] = []
    for message_id in include_message_ids or []:
        normalized = str(message_id or "").strip()
        if not normalized or normalized in seen_message_ids:
            continue
        target = get_group_msg_by_message_id(group_id, normalized)
        if target is None:
            continue
        extras.append(target)
        seen_message_ids.add(normalized)
    if not extras:
        return messages
    merged = extras + messages
    merged.sort(key=lambda msg: (float(msg.get("time", 0) or 0), str(msg.get("message_id", "") or "")))
    return merged


def set_group_style(group_id: str, style: str):
    _upsert_chat_meta(group_id, lambda group_data: group_data.__setitem__("style", style))


def get_group_style(group_id: str) -> str:
    data = _load_chat_meta()
    group_data = data.get(group_id)
    if not isinstance(group_data, dict):
        return ""
    return str(group_data.get("style", "") or "")


def get_group_topic_summary(group_id: str) -> str:
    data = _load_chat_meta()
    group_data = data.get(group_id)
    if not isinstance(group_data, dict):
        return ""
    summary = str(group_data.get("topic_summary", "") or "")
    if not summary:
        return ""

    expire_hours = _get_group_summary_expire_hours()
    if expire_hours <= 0:
        return summary

    now_ts = time.time()
    summary_ts = float(group_data.get("topic_summary_at", 0) or 0)
    if summary_ts and now_ts - summary_ts > expire_hours * 3600:
        return ""

    recent_user_msgs = [msg for msg in get_recent_group_msgs(group_id, limit=20, expire_hours=expire_hours) if not msg.get("is_bot")]
    if recent_user_msgs:
        latest_msg_ts = float(recent_user_msgs[-1].get("time", 0) or 0)
        if latest_msg_ts and now_ts - latest_msg_ts > expire_hours * 3600:
            return ""

    return summary


def set_group_topic_summary(group_id: str, summary: str, ts: float) -> None:
    def _mutator(group_data: dict[str, Any]) -> None:
        group_data["topic_summary"] = summary
        group_data["topic_summary_at"] = float(ts)

    _upsert_chat_meta(group_id, _mutator)


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


def set_group_tts_enabled(group_id: str, enabled: bool):
    def _mutate(current: object) -> Dict[str, dict]:
        configs = current if isinstance(current, dict) else {}
        group_config = configs.get(group_id)
        if not isinstance(group_config, dict):
            group_config = {}
            configs[group_id] = group_config
        group_config["tts_enabled"] = enabled
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
