from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .qq_expression_library import QQExpressionRender, render_qq_expression_message


QQ_EXPRESSION_TEST_MARKERS = {
    "face": "[QQ表情:笑哭]",
    "super": "[QQ超级表情:笑哭]",
    "favorite": "[QQ收藏表情:随机]",
}

_KIND_ALIASES = {
    "face": "face",
    "qq_face": "face",
    "small": "face",
    "normal": "face",
    "小黄脸": "face",
    "普通表情": "face",
    "super": "super",
    "super_face": "super",
    "qq_super": "super",
    "超级表情": "super",
    "favorite": "favorite",
    "fav": "favorite",
    "custom": "favorite",
    "qq_favorite": "favorite",
    "收藏表情": "favorite",
}


@dataclass(frozen=True)
class QQExpressionDiagnosticResult:
    ok: bool
    kind: str
    marker: str
    render: QQExpressionRender | None = None
    error: str = ""


def normalize_qq_expression_test_kind(value: Any) -> str:
    key = str(value or "").strip().lower()
    key = key.replace("-", "_").replace(" ", "_")
    normalized = _KIND_ALIASES.get(key)
    if normalized:
        return normalized
    if str(value or "").strip() in _KIND_ALIASES:
        return _KIND_ALIASES[str(value or "").strip()]
    raise ValueError("未知 QQ 表情测试类型，可选：小黄脸 / 超级表情 / 收藏表情")


async def build_qq_expression_test_message(
    kind: Any,
    *,
    message_segment_cls: Any,
    bot: Any = None,
    plugin_config: Any = None,
    logger: Any = None,
) -> QQExpressionDiagnosticResult:
    try:
        normalized = normalize_qq_expression_test_kind(kind)
    except ValueError as exc:
        return QQExpressionDiagnosticResult(ok=False, kind=str(kind or ""), marker="", error=str(exc))
    marker = QQ_EXPRESSION_TEST_MARKERS[normalized]
    rendered = await render_qq_expression_message(
        marker,
        message_segment_cls=message_segment_cls,
        bot=bot,
        plugin_config=plugin_config,
        logger=logger,
    )
    if rendered.message:
        return QQExpressionDiagnosticResult(ok=True, kind=normalized, marker=marker, render=rendered)
    if normalized == "favorite":
        error = "收藏表情测试失败：协议端未返回 fetch_custom_face URL，或 QQ 表情功能/协议扩展已关闭。"
    else:
        error = "QQ 表情测试失败：未渲染出可发送消息，请检查 personification_qq_expression_enabled。"
    return QQExpressionDiagnosticResult(ok=False, kind=normalized, marker=marker, render=rendered, error=error)


__all__ = [
    "QQ_EXPRESSION_TEST_MARKERS",
    "QQExpressionDiagnosticResult",
    "build_qq_expression_test_message",
    "normalize_qq_expression_test_kind",
]
