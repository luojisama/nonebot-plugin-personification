"""沉默场景的轻量回应：贴表情 / 主动拍一拍。

真群友最高频的互动不是说话，而是给消息贴个表情。本模块在 intent/LLM 判定
NO_REPLY 时按概率把"沉默"升级成"贴表情"，不新增任何 LLM 调用：
表情用关键词启发式从内置 情绪→face_id 表中选取。

风控三重闸：概率、同群冷却、每群每日上限；协议端不支持时静默降级。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from ...core import protocol_capabilities

# QQ face id（对照 coolq face id 表）：只用语义明确的常见表情。
_FACES_BY_MOOD: dict[str, tuple[int, ...]] = {
    "praise": (76, 124, 99),       # 强/OK/鼓掌
    "funny": (182, 28, 101, 13),   # 笑哭/憨笑/坏笑/呲牙
    "love": (66, 109),             # 爱心/亲亲
    "sad": (5, 9, 106, 111),       # 流泪/大哭/委屈/可怜
    "surprise": (26, 32, 97),      # 惊恐/疑问/擦汗
    "generic": (76, 182, 13, 14),  # 强/笑哭/呲牙/微笑
}

_MOOD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("funny", re.compile(r"哈哈|hhh|草|笑死|乐|🤣|😂|绷不住|蚌埠")),
    ("praise", re.compile(r"牛|强|厉害|可以啊|👍|nb|тql|tql|666|赢")),
    ("sad", re.compile(r"呜|难过|哭|惨|寄|emo|唉|😭|心碎")),
    ("surprise", re.compile(r"？？|\?\?|啊\?|什么鬼|离谱|震惊|没想到")),
    ("love", re.compile(r"爱了|可爱|喜欢|贴贴|抱抱|❤|😘|mua")),
)

_COOLDOWN_SECONDS = 60.0

# group_id -> 上次贴表情时间
_last_reaction_ts: dict[str, float] = {}
# (group_id, date) -> 当日次数
_daily_counts: dict[tuple[str, str], int] = {}
# (group_id, date) -> 当日主动拍一拍次数
_daily_poke_counts: dict[tuple[str, str], int] = {}

_PROACTIVE_POKE_PROBABILITY = 0.15
_PROACTIVE_POKE_MIN_FAV = 85.0
_PROACTIVE_POKE_DAILY_LIMIT = 3


def pick_face_id(message_text: str, rng: random.Random | None = None) -> int:
    r = rng or random
    text = str(message_text or "")
    for mood, pattern in _MOOD_PATTERNS:
        if pattern.search(text):
            return r.choice(_FACES_BY_MOOD[mood])
    return r.choice(_FACES_BY_MOOD["generic"])


def _today(now_fn: Any = time.time) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now_fn()))


def _reaction_allowed(plugin_config: Any, group_id: str, *, now_fn: Any = time.time, rng: Any = random) -> bool:
    if not bool(getattr(plugin_config, "personification_humanize_reaction_enabled", True)):
        return False
    probability = float(getattr(plugin_config, "personification_humanize_reaction_probability", 0.25) or 0.0)
    if probability <= 0.0 or rng.random() >= probability:
        return False
    now = now_fn()
    if now - _last_reaction_ts.get(str(group_id), 0.0) < _COOLDOWN_SECONDS:
        return False
    daily_limit = int(getattr(plugin_config, "personification_humanize_reaction_daily_limit", 20) or 0)
    key = (str(group_id), _today(now_fn))
    if daily_limit > 0 and _daily_counts.get(key, 0) >= daily_limit:
        return False
    return True


async def maybe_react_on_silence(
    bot: Any,
    runtime: Any,
    *,
    event: Any,
    message_text: str,
    group_id: str,
    user_id: str,
    is_private: bool,
    favorability: float = 0.0,
    rng: Any = random,
    now_fn: Any = time.time,
) -> bool:
    """NO_REPLY 时尝试贴表情（失败再考虑主动拍一拍）；返回是否做了回应。

    never-raise：任何异常只记 debug 日志。
    """
    if is_private:
        return False
    message_id = getattr(event, "message_id", None)
    plugin_config = runtime.plugin_config
    try:
        if message_id is not None and _reaction_allowed(plugin_config, group_id, now_fn=now_fn, rng=rng):
            face_id = pick_face_id(message_text, rng=rng if isinstance(rng, random.Random) else None)
            ok = await protocol_capabilities.emoji_react(
                bot,
                plugin_config,
                message_id=message_id,
                face_id=face_id,
                group_id=str(group_id),
                logger=runtime.logger,
            )
            if ok:
                now = now_fn()
                _last_reaction_ts[str(group_id)] = now
                key = (str(group_id), _today(now_fn))
                _daily_counts[key] = _daily_counts.get(key, 0) + 1
                runtime.logger.info(f"拟人插件：NO_REPLY 改为贴表情 face={face_id} group={group_id}")
                return True
        if (
            bool(getattr(plugin_config, "personification_humanize_proactive_poke_enabled", False))
            and favorability >= _PROACTIVE_POKE_MIN_FAV
            and rng.random() < _PROACTIVE_POKE_PROBABILITY
        ):
            key = (str(group_id), _today(now_fn))
            if _daily_poke_counts.get(key, 0) < _PROACTIVE_POKE_DAILY_LIMIT:
                ok = await protocol_capabilities.poke(
                    bot,
                    plugin_config,
                    user_id=user_id,
                    group_id=str(group_id),
                    logger=runtime.logger,
                )
                if ok:
                    _daily_poke_counts[key] = _daily_poke_counts.get(key, 0) + 1
                    runtime.logger.info(f"拟人插件：沉默场景改为主动拍一拍 user={user_id} group={group_id}")
                    return True
    except Exception as exc:
        runtime.logger.debug(f"[reaction] 沉默回应失败: {exc}")
    return False


async def maybe_poke_back(
    bot: Any,
    runtime: Any,
    *,
    group_id: str,
    user_id: str,
    rng: Any = random,
) -> bool:
    """被拍后按概率拍回去；与文本回复并行，不互斥。"""
    plugin_config = runtime.plugin_config
    probability = float(getattr(plugin_config, "personification_humanize_poke_back_probability", 0.3) or 0.0)
    if probability <= 0.0 or rng.random() >= probability:
        return False
    gid = str(group_id or "")
    if gid in {"None", "0"} or gid.startswith("private_"):
        gid = ""
    try:
        ok = await protocol_capabilities.poke(
            bot,
            plugin_config,
            user_id=user_id,
            group_id=gid,
            logger=runtime.logger,
        )
        if ok:
            runtime.logger.info(f"拟人插件：拍回去 user={user_id} group={group_id}")
        return ok
    except Exception as exc:
        runtime.logger.debug(f"[reaction] 拍回去失败: {exc}")
        return False


def reset_reaction_state() -> None:
    """测试用：清空冷却与日限状态。"""
    _last_reaction_ts.clear()
    _daily_counts.clear()
    _daily_poke_counts.clear()


__all__ = [
    "maybe_react_on_silence",
    "maybe_poke_back",
    "pick_face_id",
    "reset_reaction_state",
]
