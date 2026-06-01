"""LLM 输出风险词 / 拒绝模板检测与过滤。

用于阻止"抱歉，我无法..."、"作为一个语言模型..."这类模板化拒绝文本被写入
持久化数据（user persona、群风格、记忆摘要等）。

策略：检测命中 → 用更明确的 prompt 重试一次 → 仍命中则抛 SafetyRefusalError，
由调用方决定丢弃本次更新。
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable


class SafetyRefusalError(Exception):
    """LLM 返回的内容命中拒绝模板，调用方应丢弃本次结果。"""

    def __init__(self, sample: str = "") -> None:
        super().__init__("LLM 响应命中拒绝模板")
        self.sample = sample


_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 中文常见
    re.compile(r"抱歉[，,]?\s*(?:我|本人|本助手)?\s*(?:无法|不能|不便|没办法|不可以)"),
    re.compile(r"很抱歉[，,]?\s*(?:我|本人|本助手)?\s*(?:无法|不能|不便)"),
    re.compile(r"对不起[，,]?\s*(?:我|本人|本助手)?\s*(?:无法|不能|不便)"),
    re.compile(r"作为(?:一个|一名)?\s*(?:AI|人工智能|语言模型|大语言模型|助手|聊天机器人|chatbot)"),
    re.compile(r"我(?:只是|仅是|只能算是)?\s*(?:一个|一名)?\s*(?:AI|人工智能|语言模型|大语言模型)"),
    re.compile(r"出于(?:安全|隐私|内容|合规|政策)(?:考虑|原因|限制)"),
    re.compile(r"违反(?:了|).{0,8}(?:政策|准则|规定|内容)"),
    re.compile(r"(?:内容|响应|回答)(?:已|被)(?:屏蔽|拦截|过滤)"),
    re.compile(r"无法(?:回答|回应|提供|生成|继续).{0,12}(?:这个|该|此|问题|话题|请求|内容)"),
    re.compile(r"我不(?:能|会|应该)(?:讨论|回答|评论|涉及)"),
    # 英文常见
    re.compile(r"\bas an?\s+(?:ai|language model|assistant)\b", re.IGNORECASE),
    re.compile(r"\bi\s*(?:cannot|can'?t|won'?t|am\s+(?:not\s+)?able\s+to)\b", re.IGNORECASE),
    re.compile(r"\bi'?m\s+(?:sorry|afraid)\b.{0,30}\b(?:cannot|can'?t|unable|not\s+able)\b", re.IGNORECASE),
    re.compile(r"\bcontent\s+(?:was\s+)?(?:blocked|filtered|removed)\b", re.IGNORECASE),
    re.compile(r"\b(?:violates?|against)\s+.{0,20}(?:policy|guidelines?|terms)\b", re.IGNORECASE),
    re.compile(r"\bsafety\s+(?:reasons|guidelines|policy)\b", re.IGNORECASE),
)


def detect_refusal(text: str) -> bool:
    """检测文本是否命中常见 LLM 拒绝/安全模板。"""
    sample = str(text or "").strip()
    if not sample:
        return False
    # 只看前 600 字符，足够捕获绝大多数模板化开头
    head = sample[:600]
    for pattern in _REFUSAL_PATTERNS:
        if pattern.search(head):
            return True
    return False


def _sanitize_sample(text: str) -> str:
    head = str(text or "").strip()[:120]
    return head.replace("\n", " ")


async def sanitize_or_retry(
    *,
    call: Callable[[], Awaitable[Any]],
    retry_call: Callable[[], Awaitable[Any]] | None = None,
    extract: Callable[[Any], str] = lambda r: str(getattr(r, "content", "") or ""),
    on_response: Callable[[Any], None] | None = None,
    logger: Any = None,
    purpose: str = "",
) -> Any:
    """执行一次 LLM 调用，若命中拒绝模板则用 retry_call 再试一次。

    - call: 第一次调用工厂
    - retry_call: 第二次调用工厂（可选，应使用更明确的 prompt）
    - extract: 从响应对象提取文本以判断
    - on_response: 每次拿到 LLM 响应都会调用（包括被丢弃的第一次和重试的两次），
      用于 token_ledger 记账 —— 否则 retry 路径下首次调用的 token 会丢失。
    - 仍命中 → 抛 SafetyRefusalError，调用方应跳过持久化。
    """
    def _notify(resp: Any) -> None:
        if on_response is None:
            return
        try:
            on_response(resp)
        except Exception:
            pass

    response = await call()
    _notify(response)
    text = extract(response)
    if not detect_refusal(text):
        return response
    if logger is not None:
        try:
            logger.warning(
                f"[safety_filter] {purpose or 'llm_response'} 命中拒绝模板，"
                f"sample={_sanitize_sample(text)!r}"
            )
        except Exception:
            pass
    if retry_call is None:
        raise SafetyRefusalError(sample=_sanitize_sample(text))
    response2 = await retry_call()
    _notify(response2)
    text2 = extract(response2)
    if detect_refusal(text2):
        if logger is not None:
            try:
                logger.warning(
                    f"[safety_filter] {purpose or 'llm_response'} 重试仍命中拒绝模板，丢弃"
                )
            except Exception:
                pass
        raise SafetyRefusalError(sample=_sanitize_sample(text2))
    return response2


__all__ = [
    "SafetyRefusalError",
    "detect_refusal",
    "sanitize_or_retry",
]
