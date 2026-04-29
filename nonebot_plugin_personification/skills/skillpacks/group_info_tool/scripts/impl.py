from __future__ import annotations

import json
from typing import Any, Callable

from plugin.personification.agent.tool_registry import AgentTool


GROUP_INFO_DESCRIPTION = """查询 bot 当前所在的群聊列表（仅限白名单群）。
适合场景：用户询问"你在哪些群"、"把某群的群号给我"、"你在 xx 群吗"。
返回字段：group_id、group_name（如能获取）。
只返回白名单群，不暴露非白名单群信息。"""


def build_group_info_tool(
    *,
    bot: Any,
    get_whitelisted_groups: Callable[[], list[str]],
    logger: Any,
) -> AgentTool:
    async def _handler(**_params: Any) -> str:
        whitelisted = [str(gid) for gid in (get_whitelisted_groups() or []) if str(gid).strip()]
        if not whitelisted:
            return "[]"

        results: list[dict[str, str]] = []
        for gid in whitelisted:
            entry: dict[str, str] = {"group_id": gid}
            try:
                info = await bot.get_group_info(group_id=int(gid))
                entry["group_name"] = str(info.get("group_name", "") or "")
            except Exception as e:
                logger.debug(f"[group_info_tool] get_group_info failed for {gid}: {e}")
            results.append(entry)
        return json.dumps(results, ensure_ascii=False)

    return AgentTool(
        name="get_group_list",
        description=GROUP_INFO_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )
