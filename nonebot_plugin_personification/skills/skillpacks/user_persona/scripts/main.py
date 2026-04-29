from __future__ import annotations

from typing import List

from plugin.personification.core import persona_service as service


async def run(messages: List[str], previous_persona: str = "") -> str:
    msg_list = [str(item) for item in (messages or []) if str(item).strip()]
    if not msg_list:
        return "请提供 messages 列表。"
    prompt = service.build_persona_prompt(msg_list, previous_persona or None)
    return prompt
