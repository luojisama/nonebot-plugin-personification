"""主动社交闸门：在 scenario 决定 draft 之后，再让 Agent 二次判断
"现在发这条合不合适"。避免 bot 显得唐突或在用户不想被打扰时推送。

返回 (allow, None, reason)：闸门只决定是否允许发送，不改写 scenario 生成的正文。
"""
from __future__ import annotations

import json
from typing import Any

_GATE_PROMPT = """你是一个主动社交闸门，负责判断 bot 是否应该在此刻主动联系用户。

[本次想发的场景]
{scenario}

[初稿文案]
{draft}

[用户信息]
- user_id: {user_id}
- 用户画像摘要：{persona_snippet}
- 当前时间：{now}

[判断维度]
- 当前时间是否打扰用户（深夜/上班高峰等需谨慎）
- 文案是否自然像真人主动，而不是模板化群发
- 文案是否与用户画像/最近互动主题贴合
- 是否会显得"为了发而发"

[输出]
严格输出 JSON：
{{
  "allow": true 或 false,
  "reason": "一句话说明"
}}
"""


async def gate_should_send(
    *,
    tool_caller: Any,
    plugin_config: Any = None,
    tool_registry: Any = None,
    agent_max_steps: int = 4,
    logger: Any,
    scenario: str,
    user_id: str,
    draft: str,
    persona_snippet: str = "",
    now_str: str = "",
) -> tuple[bool, str | None, str]:
    """二次判断；失败默认 allow。兼容返回三元组，但第二项始终为 None。"""
    if not tool_caller:
        return True, None, "no tool_caller, skip gate"
    prompt = _GATE_PROMPT.format(
        scenario=scenario,
        draft=draft,
        user_id=user_id,
        persona_snippet=(persona_snippet or "<无>")[:200],
        now=now_str or "",
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        if tool_registry is not None:
            from ...core.agent_bridge import run_text_agent

            text = await run_text_agent(
                messages=messages,
                plugin_config=plugin_config,
                logger=logger,
                tool_caller=tool_caller,
                registry=tool_registry,
                max_steps=agent_max_steps,
                trigger_reason=f"social_gate_{scenario}",
                chat_intent_hint="social_gate",
                structured_output=True,
            )
        else:
            response = await tool_caller.chat_with_tools(
                messages=messages,
                tools=[],
                use_builtin_search=False,
            )
            text = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        logger.debug(f"[social_gate] LLM call failed, default allow: {exc}")
        return True, None, f"gate llm failed: {exc}"
    if not text:
        return True, None, "gate empty response, default allow"
    parsed = _parse_gate_json(text)
    if parsed is None:
        return True, None, f"gate non-json, default allow; raw={text[:80]}"
    allow = bool(parsed.get("allow", True))
    reason = str(parsed.get("reason", "") or "").strip()
    return allow, None, reason


def _parse_gate_json(text: str) -> dict | None:
    """从 LLM 输出里抽出 JSON。容忍 ```json fence 与多余文字。"""
    raw = text.strip()
    if raw.startswith("```"):
        # 去 markdown fence
        lines = raw.splitlines()
        if len(lines) >= 2:
            raw = "\n".join(lines[1:])
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")].strip()
    # 直接尝试
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:
        pass
    # 找第一个 { 与最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        try:
            d = json.loads(raw[start : end + 1])
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


__all__ = ["gate_should_send"]
