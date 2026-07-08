"""从用户消息中提取"未完成的事"（pending topic）。

两段式：
1. pre_filter(text)：纯正则关键短语筛，快、零 LLM 成本。不匹配就直接跳。
2. extract_pending_topic(text, now_ts, tool_caller)：匹配的消息优先走完整 Agent
   抽 (topic, time_hint_ts, confidence)，confidence < 0.5 丢弃。

设计上让 hook 把热路径开销控到极低（正则 → 99% 早退），只对真正的
"承诺/计划/未来事件"才花 LLM 一次调用。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

# pre-filter 关键词：覆盖中文里"将要做某事"的常见说法
_PREFILTER_PATTERNS = [
    re.compile(r"我(要|想|打算|准备|计划|打算去|得去|要去)"),
    re.compile(r"(明天|后天|大后天|下周|下个?月|周末|周[一二三四五六日天])"),
    re.compile(r"(几天后|几小时后|过几天|过两天|等会|晚点|稍后)"),
    re.compile(r"(出差|考试|面试|搬家|约|约好|约了|开会)"),
    re.compile(r"(\d+\s*(天|小时|周|月))[内后]"),
]


def pre_filter(text: str) -> bool:
    """快速判断这条消息是否"看起来含未完成的事"。误报可以，漏报最小化。"""
    if not text or len(text) < 4:
        return False
    if len(text) > 400:
        # 太长的消息（图片描述、转发等）误判率太高，直接跳
        return False
    return any(p.search(text) for p in _PREFILTER_PATTERNS)


_EXTRACT_PROMPT = """从用户的下面这句话里提取"未完成的承诺/计划/未来事件"，方便我之后主动关心他。

[用户当前时间]
{now_str}

[用户说]
{text}

[提取规则]
- 必须是"将来要做的事"，不是已经做完的
- 必须有时间线索（明天/下周/几天后等）或明确事件标记（出差/考试/面试）
- 不要把日常陈述（"我现在吃饭"）当成 pending
- 不要把模糊愿望（"我想去日本"）当成 pending，除非有具体时间
- 不确定就 confidence < 0.5

[输出严格 JSON]
{{
  "is_pending": true 或 false,
  "topic": "简短描述未来要做的事，10-20 字",
  "time_hint": "ISO8601 时间，估计这件事会发生的大致时间；模糊就给那天/那周的代表时间点（如下周三 = 取下周三 09:00 当地时间）",
  "raw_quote": "用户原话里跟这件事相关的最关键片段，原文照抄",
  "confidence": 0.0-1.0
}}

如果不是 pending，is_pending=false，其他字段可为空，confidence=0.0。"""


async def extract_pending_topic(
    text: str,
    *,
    now: datetime | None = None,
    tool_caller: Any,
    plugin_config: Any = None,
    tool_registry: Any = None,
    agent_max_steps: int = 4,
    logger: Any,
) -> dict | None:
    """让 LLM 抽 pending topic。失败返回 None。

    返回 dict：{topic, time_hint_ts, raw_quote, confidence}；如果 LLM
    判断不是 pending 或 confidence 不够，也返回 None。
    """
    if not tool_caller:
        return None
    now_dt = now or datetime.now()
    prompt = _EXTRACT_PROMPT.format(
        now_str=now_dt.strftime("%Y-%m-%d %H:%M (%A)"),
        text=text[:300],
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        if tool_registry is not None:
            from ...core.agent_bridge import run_text_agent

            content = await run_text_agent(
                messages=messages,
                plugin_config=plugin_config,
                logger=logger,
                tool_caller=tool_caller,
                registry=tool_registry,
                max_steps=agent_max_steps,
                trigger_reason="social_topic_extract",
                chat_intent_hint="social_topic_extract",
            )
        else:
            response = await tool_caller.chat_with_tools(
                messages=messages,
                tools=[],
                use_builtin_search=False,
            )
            content = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        logger.debug(f"[topic_extract] LLM call failed: {exc}")
        return None
    parsed = _parse_json_object(content)
    if not parsed:
        return None
    if not bool(parsed.get("is_pending")):
        return None
    confidence = _safe_float(parsed.get("confidence", 0))
    if confidence < 0.5:
        return None
    raw_quote = str(parsed.get("raw_quote", "") or "").strip()
    topic = str(parsed.get("topic", "") or "").strip()
    if not raw_quote or not topic:
        return None
    time_hint_ts = _parse_iso_to_ts(str(parsed.get("time_hint", "") or ""), fallback=now_dt)
    return {
        "topic": topic,
        "raw_quote": raw_quote,
        "time_hint_ts": time_hint_ts,
        "confidence": confidence,
    }


def _parse_json_object(text: str) -> dict | None:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:])
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")].strip()
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        try:
            d = json.loads(raw[start : end + 1])
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_to_ts(iso: str, *, fallback: datetime) -> float:
    if not iso:
        return (fallback + timedelta(days=1)).timestamp()
    try:
        cleaned = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return time.mktime(dt.timetuple())
    except Exception:
        return (fallback + timedelta(days=1)).timestamp()


__all__ = ["pre_filter", "extract_pending_topic"]
