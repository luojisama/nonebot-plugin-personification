from __future__ import annotations

import json

from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run(group_ids: list[str] | None = None, group_name_map: dict | None = None) -> str:
    ids = [str(item).strip() for item in (group_ids or []) if str(item).strip()]
    names = group_name_map if isinstance(group_name_map, dict) else {}
    rows = []
    for gid in ids:
        row = {"group_id": gid}
        if gid in names:
            row["group_name"] = str(names.get(gid) or "")
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)


def build_group_info_tool_for_runtime(*, bot, runtime: SkillRuntime):
    return impl.build_group_info_tool(
        bot=bot,
        get_whitelisted_groups=runtime.get_whitelisted_groups,  # type: ignore[attr-defined]
        logger=runtime.logger,
    )

