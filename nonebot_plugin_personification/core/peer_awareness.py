"""Bot 对环境的感知：

1. **其他 NoneBot 插件清单**：从 `nonebot.get_loaded_plugins()` 收集
   插件名 + 一句话描述，注入 responder system prompt，避免 bot 把别人的功能
   说成是自己的。
2. **其他 bot / Q 群管家识别**：用启发式判断当前消息或最近上文是否来自
   另一个机器人，命中时建议本轮静默，避免 bot 互相对话。

设计要点：所有公开函数对 import 错误、运行时异常都要 silently degrade，
不能在缺 NoneBot 上下文时阻塞 responder 启动。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterable


_PLUGIN_LIST_CACHE: dict[str, Any] = {"ts": 0.0, "items": []}
_PLUGIN_CACHE_TTL_SECONDS = 60.0


def _safe_first_line(text: str, *, limit: int = 40) -> str:
    head = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    head = head.strip()
    if not head:
        return ""
    return head[:limit]


def _extract_plugin_meta(plugin: Any) -> dict[str, str] | None:
    """从 NoneBot Plugin 对象抽取展示用 metadata（name + 简短描述）。"""
    try:
        module = getattr(plugin, "module", None)
        module_name = str(getattr(module, "__name__", "") or "")
        if not module_name or module_name.startswith("nonebot.plugins"):
            return None
        name = str(getattr(plugin, "name", "") or module_name.split(".")[-1]).strip()
        if not name or name == "personification" or module_name.endswith(".personification"):
            return None
        # 优先使用 plugin_meta.PluginMetadata
        description = ""
        metadata = getattr(plugin, "metadata", None)
        if metadata is not None:
            description = _safe_first_line(getattr(metadata, "description", "") or "")
            if not description:
                description = _safe_first_line(getattr(metadata, "usage", "") or "")
        if not description and module is not None:
            description = _safe_first_line(getattr(module, "__doc__", "") or "")
        return {"name": name, "description": description}
    except Exception:
        return None


def list_other_plugins(*, max_plugins: int = 12) -> list[dict[str, str]]:
    """枚举当前 NoneBot 加载的其他插件（不含 personification）。带 60s 缓存。"""
    now = time.time()
    if (now - float(_PLUGIN_LIST_CACHE.get("ts", 0) or 0)) < _PLUGIN_CACHE_TTL_SECONDS:
        cached = _PLUGIN_LIST_CACHE.get("items")
        if isinstance(cached, list):
            return cached[:max_plugins]
    items: list[dict[str, str]] = []
    try:
        import nonebot

        for plugin in list(nonebot.get_loaded_plugins() or []):
            entry = _extract_plugin_meta(plugin)
            if entry:
                items.append(entry)
    except Exception:
        items = []
    # 去重 by name
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for entry in items:
        n = entry["name"]
        if n in seen:
            continue
        seen.add(n)
        deduped.append(entry)
    _PLUGIN_LIST_CACHE["ts"] = now
    _PLUGIN_LIST_CACHE["items"] = deduped
    return deduped[:max_plugins]


def render_other_plugins_hint(*, max_plugins: int = 10) -> str:
    """生成可注入 system prompt 的"已知其他插件"段落。无插件时返回空串。"""
    items = list_other_plugins(max_plugins=max_plugins)
    if not items:
        return ""
    lines = ["## 你身边的其他插件（仅供识别用）"]
    for entry in items:
        name = entry["name"]
        desc = entry.get("description") or ""
        if desc:
            lines.append(f"- {name}：{desc}")
        else:
            lines.append(f"- {name}")
    lines.append(
        "规则：用户问到上面任何一个插件名时，明确指向那个插件，不要把它的功能"
        "说成是 personification（你自己）做的；插件名不在清单内时，直说"
        "『不太了解这个插件』并建议用户去问对应插件。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 其他 bot / Q 群管家识别
# ---------------------------------------------------------------------------

# Tencent 官方 Q 群管家 / 群通知机器人 user_id 模式（实际部署中可由配置覆盖）。
_KNOWN_BOT_USER_IDS: set[str] = {
    "2854196306",  # Q 群管家公开 ID
    "2854196311",
    "1840000000",  # 公司机器人通用号段（保留）
}

# 启发式：典型管家/系统通报开头。
# 注意：检测前会先把 text 用 _strip_decoration() 剥掉前导空白 / emoji /
# 装饰括号块（如 "【群通知】"），让 ^ 锚点匹配真实开头。
# Chinese 不使用 \b 词边界（CJK 字符相邻都被认为是 word char，\b 不触发）。
_BOT_LIKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^欢迎(?:新成员|新人)"),
    re.compile(r"^我已加入"),
    re.compile(r"^(?:今日|本群)(?:精华|榜单|签到)"),
    re.compile(r"^群积分\s*\+"),
    re.compile(r"\[Q群管家\]"),
    re.compile(r"^/(?:help|签到|sign|info|stat)"),
    re.compile(r"^本群.{0,8}已开启"),
    re.compile(r"^(?:签到成功|打卡成功)"),
)

# 去除消息前的装饰：空白和常见 emoji / symbol（用 Unicode block 范围而不是
# 枚举具体字符，避免源码里嵌入 variation-selector / ZWJ 这类不可见控制符）。
# Symbol-and-pictograph 块: U+2600-U+27BF（杂项符号 + dingbats），
# U+1F300-U+1FAFF（emoji 主块）。
_DECORATION_RE = re.compile(
    r"^[\s☀-➿\U0001f300-\U0001faff]+"
)

# 剥离一对前导通知括号（含 ≤14 字内部内容）：【群通知】、[系统]、(notice)、〔提示〕。
# 但保留 [Q群管家] 这类我们用作匹配标记的串，所以用 negative lookahead 跳过。
_PREFIX_BRACKET_RE = re.compile(
    r"^[\[【〔(](?!Q群管家)[^\]】〕)]{0,14}[\]】〕)]\s*"
)


def _strip_decoration(text: str) -> str:
    cleaned = str(text or "")
    # 反复剥离 emoji/空白 + 通知括号块，直到稳定。最多 4 轮避免病态输入死循环。
    for _ in range(4):
        stripped = _DECORATION_RE.sub("", cleaned)
        stripped = _PREFIX_BRACKET_RE.sub("", stripped).lstrip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


@dataclass(frozen=True)
class PeerBotDecision:
    is_other_bot: bool
    reason: str = ""
    suggest_silence: bool = False


def detect_other_bot(
    *,
    user_id: str = "",
    text: str = "",
    extra_bot_ids: Iterable[str] | None = None,
) -> PeerBotDecision:
    """判断当前消息是否来自另一个机器人 / Q 群管家。

    user_id 为空 / extra_bot_ids 含空串时不会误命中；
    text 在匹配前会剥离前导装饰（空白 / emoji / 通知括号块）。
    """
    uid = str(user_id or "").strip()
    if uid:
        if uid in _KNOWN_BOT_USER_IDS:
            return PeerBotDecision(is_other_bot=True, reason="known_bot_uid", suggest_silence=True)
        if extra_bot_ids:
            extras = {x for x in (str(item or "").strip() for item in extra_bot_ids) if x}
            if uid in extras:
                return PeerBotDecision(is_other_bot=True, reason="config_bot_uid", suggest_silence=True)
    snippet = _strip_decoration(text)
    if snippet:
        for pat in _BOT_LIKE_PATTERNS:
            if pat.search(snippet):
                return PeerBotDecision(is_other_bot=True, reason="bot_like_pattern", suggest_silence=True)
    return PeerBotDecision(is_other_bot=False)


__all__ = [
    "PeerBotDecision",
    "detect_other_bot",
    "list_other_plugins",
    "render_other_plugins_hint",
]
