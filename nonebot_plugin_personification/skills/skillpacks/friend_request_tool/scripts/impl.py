from __future__ import annotations

import json
import time
from typing import Any, Callable

from plugin.personification.agent.tool_registry import AgentTool
from plugin.personification.core.data_store import get_data_store
from plugin.personification.core.time_ctx import get_current_day_str


FRIEND_REQUEST_DESCRIPTION = """向指定用户发送好友申请。
仅在你真心想认识对方、且对话让你感觉很投缘时才调用。
不要因为对方问了你问题就申请，要有真实的情感驱动。
调用前请确保你已经和对方聊了好几轮，有一定了解。"""

_STORE_NAME = "friend_request_state"


def _load_state(plugin_config: Any | None = None) -> dict:
    _ = plugin_config
    data = get_data_store().load_sync(_STORE_NAME)
    return data if isinstance(data, dict) else {}


def _save_state(state: dict, plugin_config: Any | None = None) -> None:
    _ = plugin_config
    get_data_store().save_sync(_STORE_NAME, state)


def check_friend_request_gate(
    *,
    plugin_config: Any,
    user_id: str,
    now_ts: float | None = None,
) -> tuple[bool, str]:
    state = _load_state(plugin_config)
    today = get_current_day_str()
    today_count_map = state.get("today_count", {})
    if not isinstance(today_count_map, dict):
        today_count_map = {}
    today_count = int(today_count_map.get(today, 0) or 0)
    daily_limit = int(getattr(plugin_config, "personification_friend_request_daily_limit", 2))
    if today_count >= daily_limit:
        return False, f"daily_limit_reached ({today_count}/{daily_limit})"

    last_req_map = state.get("last_request_per_user", {})
    if not isinstance(last_req_map, dict):
        last_req_map = {}
    last_req_ts = float(last_req_map.get(user_id, 0) or 0)
    current_ts = float(now_ts or time.time())
    cooldown_days = 7
    if last_req_ts and (current_ts - last_req_ts) < cooldown_days * 86400:
        elapsed = int((current_ts - last_req_ts) / 86400)
        return False, f"cooldown ({elapsed}d < {cooldown_days}d)"

    return True, ""


def build_friend_request_tool(
    *,
    bot: Any,
    plugin_config: Any,
    get_user_data: Callable[[str], dict],
    get_friend_ids: Callable[[], set[str]],
    logger: Any,
    session_interaction_count: int,
    is_group_scene: bool,
) -> AgentTool:
    async def _handler(
        user_id: str,
        comment: str,
        interaction_count: int,
    ) -> str:
        if not getattr(plugin_config, "personification_friend_request_enabled", False):
            return json.dumps({"ok": False, "reason": "friend_request_disabled"}, ensure_ascii=False)

        if not is_group_scene:
            return json.dumps({"ok": False, "reason": "group_only"}, ensure_ascii=False)

        user_id = str(user_id or "").strip()
        comment = str(comment or "你好，想加你为好友～").strip()[:50]
        try:
            requested_count = max(0, int(str(interaction_count or 0).strip()))
        except Exception:
            requested_count = 0
        actual_interaction_count = max(0, int(session_interaction_count))

        if not user_id:
            return json.dumps({"ok": False, "reason": "missing_user_id"}, ensure_ascii=False)

        user_data = get_user_data(user_id)
        if user_data.get("is_perm_blacklisted"):
            return json.dumps({"ok": False, "reason": "user_blacklisted"}, ensure_ascii=False)

        fav = float(user_data.get("favorability", 0.0) or 0.0)
        min_fav = float(getattr(plugin_config, "personification_friend_request_min_fav", 85.0))
        if fav < min_fav:
            return json.dumps(
                {"ok": False, "reason": f"favorability_too_low ({fav:.1f} < {min_fav:.1f})"},
                ensure_ascii=False,
            )

        if actual_interaction_count < 3:
            return json.dumps(
                {
                    "ok": False,
                    "reason": (
                        f"too_few_interactions ({actual_interaction_count}; requested={requested_count})"
                    ),
                },
                ensure_ascii=False,
            )

        friend_ids = {str(item) for item in (get_friend_ids() or set())}
        if user_id in friend_ids:
            return json.dumps({"ok": False, "reason": "already_friend"}, ensure_ascii=False)

        now_ts = time.time()
        gate_ok, gate_reason = check_friend_request_gate(
            plugin_config=plugin_config,
            user_id=user_id,
            now_ts=now_ts,
        )
        if not gate_ok:
            return json.dumps({"ok": False, "reason": gate_reason}, ensure_ascii=False)

        state = _load_state(plugin_config)
        today = get_current_day_str()
        today_count_map = state.get("today_count", {})
        if not isinstance(today_count_map, dict):
            today_count_map = {}
        today_count = int(today_count_map.get(today, 0) or 0)
        last_req_map = state.get("last_request_per_user", {})
        if not isinstance(last_req_map, dict):
            last_req_map = {}

        sent = False
        last_error = ""
        for method_name, kwargs in (
            ("send_friend_request", {"user_id": int(user_id), "comment": comment}),
            ("add_friend", {"user_id": int(user_id), "comment": comment}),
            ("friend_add", {"user_id": int(user_id), "remark": comment}),
        ):
            method = getattr(bot, method_name, None)
            if method is None:
                continue
            try:
                await method(**kwargs)
                sent = True
                break
            except Exception as e:
                last_error = f"{method_name}: {e}"

        if not sent:
            return json.dumps(
                {"ok": False, "reason": f"send_failed ({last_error or 'no_supported_api'})"},
                ensure_ascii=False,
            )

        today_count_map[today] = today_count + 1
        last_req_map[user_id] = now_ts
        state["today_count"] = today_count_map
        state["last_request_per_user"] = last_req_map
        _save_state(state, plugin_config)

        logger.info(f"[friend_request] 已向用户 {user_id} 发送好友申请，附言：{comment}")
        return json.dumps({"ok": True, "user_id": user_id}, ensure_ascii=False)

    return AgentTool(
        name="send_friend_request",
        description=FRIEND_REQUEST_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "目标用户的 QQ 号",
                },
                "comment": {
                    "type": "string",
                    "description": "好友申请附言，用角色口吻写，不超过50字",
                },
                "interaction_count": {
                    "type": "integer",
                    "description": "本次会话中与该用户的互动轮次，由调用方统计后传入",
                },
            },
            "required": ["user_id", "comment", "interaction_count"],
        },
        handler=_handler,
    )
