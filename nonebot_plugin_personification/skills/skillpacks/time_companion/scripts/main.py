from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _pick_tip(hour: int) -> str:
    if 0 <= hour < 6:
        return "夜深了，记得补充睡眠。"
    if 6 <= hour < 11:
        return "早上适合推进最重要的一件事。"
    if 11 <= hour < 14:
        return "中午别忘了吃饭和短暂放松。"
    if 14 <= hour < 18:
        return "下午适合处理沟通和收尾任务。"
    if 18 <= hour < 22:
        return "晚上可以做轻松但有成就感的事。"
    return "今天辛苦啦，准备慢慢收工吧。"


def _apply_mood(text: str, mood: str) -> str:
    style = (mood or "").strip().lower()
    if style == "energetic":
        return text + " 冲一小步就很棒。"
    if style == "calm":
        return text + " 慢一点也没关系。"
    return text + " 你按自己的节奏来就好。"


async def run(timezone: str = "Asia/Shanghai", mood: str = "gentle") -> str:
    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        timezone = "Asia/Shanghai"
    tip = _pick_tip(now.hour)
    base = f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（{timezone}）。{tip}"
    return _apply_mood(base, mood)
