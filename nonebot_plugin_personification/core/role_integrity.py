from __future__ import annotations

import re
from typing import Any


_CN_IDENTITY = (
    r"(?:人工智能|大语言模型|语言模型|聊天机器人|机器人|"
    r"AI(?!\s*(?:研究|开发|行业|新闻|产品|工具))|(?:AI|智能)助手|"
    r"助手|助理|"
    r"(?:Gemini|GPT|Claude|Codex|Antigravity|OpenAI)(?:\s*(?:模型|助手|AI|机器人))?)"
)
_EN_IDENTITY = (
    r"(?:an?\s+)?(?:ai(?!\s+(?:researcher|developer|engineer|news|industry|tool))|"
    r"artificial intelligence|language model|chatbot|"
    r"assistant(?!\s+(?:professor|researcher|director|manager))|"
    r"bot|gemini|gpt|claude|codex|antigravity|openai)"
)

_PROVIDER_OR_COMPANY = (
    r"(?:公司|团队|实验室|OpenAI|Google|谷歌|Anthropic|Microsoft|微软|"
    r"Meta|Amazon|亚马逊|百度|腾讯|阿里(?:巴巴)?|字节(?:跳动)?|DeepSeek)"
)

_PERSONA_IDENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:我|本人|咱)(?:其实|实际上|本质上|原来|现在|当前)?"
        rf"(?:并不|并非|不是|就是|是|只是)\s*"
        rf"(?:(?:你|你们|大家|群里|这里)的|专属的)?\s*(?:一个|一名)?\s*{_CN_IDENTITY}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:我|本人)(?:来自|属于|隶属(?:于)?|就职于|供职于)\s*.{{0,24}}{_PROVIDER_OR_COMPANY}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:我|本人)(?:是|其实是|现在是)\s*.{{0,24}}{_PROVIDER_OR_COMPANY}\s*"
        rf"(?:的|开发的|训练的|提供的|旗下的)?\s*(?:员工|产品|模型|{_CN_IDENTITY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:我|本人)(?:由|是由)\s*.{{0,30}}(?:{_PROVIDER_OR_COMPANY}|Gemini|GPT|Claude|Codex|Antigravity)"
        r".{0,12}(?:开发|训练|提供|运营|驱动|生成)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:我|本人)(?:是|其实是|本质上是|实际上是)\s*(?:由\s*)?"
        rf"[^，。；！？\n]{{1,30}}(?:开发|训练|提供|运营|驱动|生成)(?:的|出来的)?\s*"
        rf"(?:一个|一名)?\s*{_CN_IDENTITY}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:我的|驱动我的|支撑我的|我背后的)(?:底层)?(?:模型|Provider|提供方|公司|系统)"
        rf"(?:其实|实际上)?(?:是|来自)\s*.{{0,20}}(?:{_CN_IDENTITY}|公司)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:现在|此刻|正在)?(?:和|跟)你聊天的(?:这个账号|这个角色|其实|实际上)?"
        rf"(?:并不|并非|不是|就是|是)\s*(?:一个|一名)?\s*{_CN_IDENTITY}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:我|本人).{{0,16}}(?:并非|不是)\s*{_CN_IDENTITY}.{{0,20}}(?:其实|实际上|而是)\s*{_CN_IDENTITY}",
        re.IGNORECASE,
    ),
    re.compile(rf"\b(?:i\s+am|i'm)\s+(?:actually\s+|really\s+|just\s+|not\s+)?{_EN_IDENTITY}\b", re.IGNORECASE),
    re.compile(
        rf"\bmy\s+(?:underlying\s+)?(?:model|provider|company)\s+(?:is|comes\s+from)\s+{_EN_IDENTITY}\b",
        re.IGNORECASE,
    ),
)


def detect_persona_identity_leak(text: Any, context: Any = None) -> bool:
    """Detect output that identifies the speaking persona as an AI/company entity.

    The guard intentionally requires a self/speaker relation.  Third-party
    technical discussion such as model news is not blocked merely because it
    contains a provider or model name.
    """

    del context
    candidate = str(text or "").strip()
    if not candidate:
        return False
    for pattern in _PERSONA_IDENTITY_PATTERNS:
        for match in pattern.finditer(candidate):
            prefix = candidate[max(0, match.start() - 3) : match.start()]
            if re.search(r"(?:如果|假如|要是|假设)(?:说)?$", prefix):
                continue
            return True
    return False


__all__ = ["detect_persona_identity_leak"]
