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
    """LLM 返回的内容命中拒绝模板或被 API 层安全策略拦截，调用方应丢弃本次结果。

    source: "text"=自生成拒绝文本；"api_block"=供应商 API 层强制拦截。
    reason: API 层拦截时的具体原因（如 gemini:SAFETY / openai:content_filter）。
    """

    def __init__(self, sample: str = "", *, source: str = "text", reason: str = "") -> None:
        super().__init__(
            f"LLM 响应被{'API 安全策略拦截' if source == 'api_block' else '命中拒绝模板'}"
            + (f"：{reason}" if reason else "")
        )
        self.sample = sample
        self.source = source
        self.reason = reason


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
    # 客服式婉拒（画像/分析类任务常见）
    re.compile(r"不在我(?:能|可以)?(?:协助|帮助|提供帮助|处理|完成)的范围"),
    re.compile(r"(?:这类|此类|这种|该)?(?:请求|任务|分析|内容).{0,6}(?:不在|超出).{0,8}范围"),
    re.compile(r"我(?:没办法|没有办法|无法|不便)(?:完成|协助|处理|继续)(?:这个|该|此)?(?:请求|任务)?"),
    re.compile(r"如果你有.{0,20}(?:问题|需求|需要).{0,6}(?:随时|请)(?:告诉|联系|问)我"),
    re.compile(r"很乐意.{0,10}(?:其他|别的).{0,6}(?:问题|方面|帮助)"),
    re.compile(r"建议(?:你)?(?:咨询|寻求|联系).{0,10}(?:专业|相关)(?:人士|人员|机构)"),
    # 英文常见
    re.compile(r"\bas an?\s+(?:ai|language model|assistant)\b", re.IGNORECASE),
    re.compile(r"\bi\s*(?:cannot|can'?t|won'?t|am\s+(?:not\s+)?able\s+to)\b", re.IGNORECASE),
    re.compile(r"\bi'?m\s+(?:sorry|afraid)\b.{0,30}\b(?:cannot|can'?t|unable|not\s+able)\b", re.IGNORECASE),
    re.compile(r"\bcontent\s+(?:was\s+)?(?:blocked|filtered|removed)\b", re.IGNORECASE),
    re.compile(r"\b(?:violates?|against)\s+.{0,20}(?:policy|guidelines?|terms)\b", re.IGNORECASE),
    re.compile(r"\bsafety\s+(?:reasons|guidelines|policy)\b", re.IGNORECASE),
)


# 触发"被拦截"判定的 finishReason / blockReason 取值（大小写无关）
_BLOCK_FINISH_REASONS: frozenset[str] = frozenset({
    "content_filter", "safety", "recitation", "blocklist",
    "prohibited_content", "spii", "image_safety", "refusal", "blocked",
})
_BLOCK_REASON_IGNORE: frozenset[str] = frozenset({"", "0", "block_reason_unspecified", "stop", "end_turn"})


def _get(obj: Any, key: str) -> Any:
    """从 dict 或对象上取属性，取不到返回 None。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def detect_api_block(response: Any) -> str:
    """检查 LLM 响应是否被供应商 API 层安全策略拦截。

    覆盖 OpenAI(finish_reason=content_filter)、Gemini(promptFeedback.blockReason /
    candidates[].finishReason=SAFETY 等)、Anthropic(stop_reason=refusal)。
    返回形如 "gemini:SAFETY" 的原因串；未被拦截返回 ""。绝不抛异常。
    """
    if response is None:
        return ""
    try:
        # 1) ToolCallerResponse.finish_reason 已被标记
        fr = _norm(_get(response, "finish_reason"))
        if fr in _BLOCK_FINISH_REASONS:
            return f"finish_reason:{fr}"

        raw = _get(response, "raw")
        if raw is None:
            raw = response

        # 2) Gemini：promptFeedback.blockReason
        feedback = _get(raw, "prompt_feedback") or _get(raw, "promptFeedback")
        block_reason = _get(feedback, "block_reason") or _get(feedback, "blockReason")
        if block_reason is not None and _norm(block_reason) not in _BLOCK_REASON_IGNORE:
            return f"gemini:{block_reason}"

        # 3) Gemini：candidates[].finishReason
        candidates = _get(raw, "candidates")
        if isinstance(candidates, (list, tuple)):
            for cand in candidates:
                raw_cfr = _get(cand, "finish_reason") or _get(cand, "finishReason")
                if _norm(raw_cfr) in _BLOCK_FINISH_REASONS:
                    return f"gemini:{raw_cfr}"

        # 4) OpenAI：choices[].finish_reason == content_filter
        choices = _get(raw, "choices")
        if isinstance(choices, (list, tuple)):
            for ch in choices:
                raw_cfr = _get(ch, "finish_reason") or _get(ch, "finishReason")
                if _norm(raw_cfr) in _BLOCK_FINISH_REASONS:
                    return f"openai:{raw_cfr}"

        # 5) Anthropic：stop_reason == refusal
        stop_reason = _norm(_get(raw, "stop_reason"))
        if stop_reason in _BLOCK_FINISH_REASONS:
            return f"anthropic:{stop_reason}"
    except Exception:
        return ""
    return ""


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
    block = detect_api_block(response)
    if not block and not detect_refusal(text):
        return response
    source = "api_block" if block else "text"
    if logger is not None:
        try:
            if block:
                logger.warning(
                    f"[safety_filter] {purpose or 'llm_response'} 被 API 安全策略拦截"
                    f"（{block}），将重试软化 prompt"
                )
            else:
                logger.warning(
                    f"[safety_filter] {purpose or 'llm_response'} 命中拒绝模板，"
                    f"sample={_sanitize_sample(text)!r}"
                )
        except Exception:
            pass
    if retry_call is None:
        raise SafetyRefusalError(sample=_sanitize_sample(text), source=source, reason=block)
    response2 = await retry_call()
    _notify(response2)
    text2 = extract(response2)
    block2 = detect_api_block(response2)
    if block2 or detect_refusal(text2):
        source2 = "api_block" if block2 else "text"
        if logger is not None:
            try:
                logger.warning(
                    f"[safety_filter] {purpose or 'llm_response'} 重试后仍"
                    f"{'被 API 拦截（' + block2 + '）' if block2 else '命中拒绝模板'}，丢弃"
                )
            except Exception:
                pass
        raise SafetyRefusalError(sample=_sanitize_sample(text2), source=source2, reason=block2)
    return response2


__all__ = [
    "SafetyRefusalError",
    "detect_refusal",
    "detect_api_block",
    "sanitize_or_retry",
]
