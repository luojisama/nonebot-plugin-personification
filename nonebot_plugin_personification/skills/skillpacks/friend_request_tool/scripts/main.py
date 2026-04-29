from __future__ import annotations

import json
from types import SimpleNamespace

from plugin.personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run_check_gate(user_id: str, daily_limit: int = 2) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        return json.dumps({"ok": False, "reason": "missing_user_id"}, ensure_ascii=False)
    cfg = SimpleNamespace(personification_friend_request_daily_limit=int(daily_limit))
    ok, reason = impl.check_friend_request_gate(plugin_config=cfg, user_id=uid)
    return json.dumps({"ok": bool(ok), "reason": str(reason)}, ensure_ascii=False)


async def run(user_id: str, daily_limit: int = 2) -> str:
    return await run_check_gate(user_id=user_id, daily_limit=daily_limit)


def build_friend_request_tool_for_runtime(
    *,
    bot,
    runtime: SkillRuntime,
    get_user_data,
    get_friend_ids,
    session_interaction_count: int,
    is_group_scene: bool,
):
    return impl.build_friend_request_tool(
        bot=bot,
        plugin_config=runtime.plugin_config,
        get_user_data=get_user_data,
        get_friend_ids=get_friend_ids,
        logger=runtime.logger,
        session_interaction_count=session_interaction_count,
        is_group_scene=is_group_scene,
    )
