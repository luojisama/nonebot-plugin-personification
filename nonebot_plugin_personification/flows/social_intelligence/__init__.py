"""Social Intelligence：主动社交框架与场景。

跟 proactive_flow 的区别：
- proactive_flow 是 "interval polling + LLM 决策今天给谁发"，整体一次决策
- social_intelligence 是 "按场景 cron/event 触发"，每个场景独立判断

各场景共享：用户白名单解析、配额节流、LLM 闸门（二次决策"现在该不该发"）。
"""
from __future__ import annotations

from .framework import SocialContext, SocialTrigger, register_social_trigger
from .scheduler import setup_social_intelligence_jobs

__all__ = [
    "SocialContext",
    "SocialTrigger",
    "register_social_trigger",
    "setup_social_intelligence_jobs",
]
