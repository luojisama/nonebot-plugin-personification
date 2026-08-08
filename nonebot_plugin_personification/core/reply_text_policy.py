from __future__ import annotations

import re
from typing import Any

from .context_policy import (
    looks_like_visible_reasoning_trace,
    strip_orphan_control_brackets,
    strip_visible_reasoning_trace,
)


_IMAGE_B64_RE = re.compile(r"\[IMAGE_B64\][A-Za-z0-9+/=\r\n]+\[/IMAGE_B64\]")
_FENCED_CODE_RE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?([\s\S]*?)```", re.MULTILINE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://|file://|/)[^)]+\)")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_SETEXT_HEADING_RE = re.compile(r"^\s*(?:={3,}|-{3,})\s*$", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.MULTILINE)
_HORIZONTAL_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_TASK_BULLET_RE = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d{1,3}[.)]\s+", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")
_LEADING_FORMULAIC_TIC_RE = re.compile(
    r"^\s*(?:等下|等一下|啊这|不是)\s*[，,、。！？!?.…]*\s*"
)
_OBSERVER_STATUS_TIC_RE = re.compile(
    r"^\s*(?:我\s*)?(?:先\s*)?"
    r"(?:(?:潜水|围观|观望|路过)(?:一下)?|蹲一下|看看情况|看下情况|看情况)"
    r"\s*[，,、。！？!?.…]*\s*"
    r"(?:(?:等(?:一会儿?|会儿?|下)?|晚点|回头)?\s*再\s*(?:说|看|聊)(?:吧)?)?"
    r"\s*[，,、。！？!?.…]*\s*"
)
_FORMULAIC_TAIBA_RE = re.compile(
    r"(?P<prefix>^|[，,。！？!?；;\n…]+\s*)"
    r"(?:这也太|这也有点|这也够|你这也太|这听着也太|这看着也太)"
    r"(?P<core>[^，,。！？!?；;\n]{1,18}?)"
    r"(?:了吧|了|吧|啊)?"
    r"(?=$|[，,。！？!?；;\n…])"
)
_FORMULAIC_ZHEYE_RE = re.compile(
    r"(?P<prefix>^|[，,。！？!?；;\n…]+\s*)"
    r"(?:这也|你这也)"
    r"(?P<core>[^，,。！？!?；;\n]{1,16}?)"
    r"(?=$|[，,。！？!?；;\n…])"
)
_QUESTION_MARK_RE = re.compile(r"[?？]")
_QUESTION_TAIL_RE = re.compile(r"(?:吗|嘛|么|呢|哪(?:儿|里|边)?|什么|怎么|咋|谁|几|多少)[啊呀嘛呢吧呗]*[。.!！…~～]*$")
_QUESTION_FRAGMENT_RE = re.compile(r"(?:哪儿|哪里|哪边|什么|怎么|咋|谁|几[点个]?|多少)[啊呀嘛呢吧呗]*[，,。！？!?]")


def _polish_formulaic_core(core: str) -> str:
    value = re.sub(r"\s+", "", str(core or "").strip())
    value = re.sub(r"^(?:太|有点|够)", "", value).strip()
    value = re.sub(r"(?:了吧|了|吧|啊)$", "", value).strip()
    if not value:
        return ""
    if value.endswith(("吗", "嘛", "呢", "呀", "啦", "么")):
        return value
    if "太" in value:
        return value.replace("太", "挺", 1)
    if len(value) <= 6 and not value.endswith(("的", "了")):
        return f"挺{value}的"
    return value


def _normalize_formulaic_reply_tics(text: str) -> str:
    """Reduce high-frequency model tics in final visible text.

    This stays in the mechanical output-style layer. It does not decide intent,
    emotion, or whether the bot should speak.
    """
    cleaned = str(text or "")
    cleaned = _OBSERVER_STATUS_TIC_RE.sub("", cleaned)
    cleaned = _LEADING_FORMULAIC_TIC_RE.sub("", cleaned)

    def _replace_taiba(match: re.Match[str]) -> str:
        prefix = str(match.group("prefix") or "")
        core = _polish_formulaic_core(str(match.group("core") or ""))
        return f"{prefix}{core}" if core else prefix

    cleaned = _FORMULAIC_TAIBA_RE.sub(_replace_taiba, cleaned)

    def _replace_zheye(match: re.Match[str]) -> str:
        prefix = str(match.group("prefix") or "")
        core = str(match.group("core") or "").strip()
        if not core:
            return prefix
        if re.search(r"(?:太|有点|够)", core):
            core = _polish_formulaic_core(core)
            return f"{prefix}{core}" if core else prefix
        return match.group(0)

    cleaned = _FORMULAIC_ZHEYE_RE.sub(_replace_zheye, cleaned)
    cleaned = re.sub(r"([，,。！？!?；;])\s*([，,。！？!?；;])+", r"\1", cleaned)
    cleaned = re.sub(r"^\s*[，,、。！？!?；;]+\s*", "", cleaned)
    return cleaned.strip()


def looks_like_formulaic_reply_tic(text: Any) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    zheye_problem = False
    for match in _FORMULAIC_ZHEYE_RE.finditer(raw):
        core = str(match.group("core") or "")
        if re.search(r"(?:太|有点|够)", core):
            zheye_problem = True
            break
    return bool(
        _OBSERVER_STATUS_TIC_RE.search(raw)
        or _LEADING_FORMULAIC_TIC_RE.search(raw)
        or _FORMULAIC_TAIBA_RE.search(raw)
        or zheye_problem
    )


def _protect_image_markers(text: str) -> tuple[str, list[str]]:
    markers: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        markers.append(match.group(0))
        return f"@@PERSONIFICATION_IMAGE_B64_{len(markers) - 1}@@"

    return _IMAGE_B64_RE.sub(_replace, text), markers


def _restore_image_markers(text: str, markers: list[str]) -> str:
    restored = text
    for index, marker in enumerate(markers):
        restored = restored.replace(f"@@PERSONIFICATION_IMAGE_B64_{index}@@", marker)
    return restored


def normalize_visible_reply_text(text: Any) -> str:
    """Strip Markdown-like formatting from user-visible chat replies.

    This is intentionally structural only: it removes presentation syntax from
    model output without trying to infer dialogue intent or topic semantics.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = strip_visible_reasoning_trace(strip_orphan_control_brackets(raw))
    if not raw:
        return ""

    cleaned, image_markers = _protect_image_markers(raw)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _FENCED_CODE_RE.sub(lambda m: str(m.group(1) or "").strip(), cleaned)
    cleaned = _MARKDOWN_IMAGE_RE.sub(lambda m: str(m.group(1) or "").strip(), cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(lambda m: str(m.group(1) or "").strip(), cleaned)
    cleaned = _INLINE_CODE_RE.sub(lambda m: str(m.group(1) or ""), cleaned)
    cleaned = _BOLD_RE.sub(lambda m: str(m.group(2) or ""), cleaned)
    cleaned = _ITALIC_STAR_RE.sub(lambda m: str(m.group(1) or ""), cleaned)
    cleaned = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", lambda m: str(m.group(1) or ""), cleaned)
    cleaned = _SETEXT_HEADING_RE.sub("", cleaned)
    cleaned = _TABLE_SEPARATOR_RE.sub("", cleaned)
    cleaned = _HORIZONTAL_RULE_RE.sub("", cleaned)
    cleaned = _HEADING_RE.sub("", cleaned)
    cleaned = _BLOCKQUOTE_RE.sub("", cleaned)
    cleaned = _TASK_BULLET_RE.sub("", cleaned)
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _NUMBERED_RE.sub("", cleaned)
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = strip_visible_reasoning_trace(strip_orphan_control_brackets(cleaned))
    cleaned = _normalize_formulaic_reply_tics(cleaned)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.split("\n")]
    compacted: list[str] = []
    blank_seen = False
    for line in lines:
        if not line:
            if compacted and not blank_seen:
                compacted.append("")
                blank_seen = True
            continue
        compacted.append(line)
        blank_seen = False
    cleaned = "\n".join(compacted).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return _restore_image_markers(cleaned, image_markers).strip()


def looks_like_markdown_reply(text: Any) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    return bool(
        looks_like_visible_reasoning_trace(raw)
        or (bool(raw.strip()) and not strip_orphan_control_brackets(raw))
        or _FENCED_CODE_RE.search(raw)
        or _MARKDOWN_LINK_RE.search(raw)
        or _MARKDOWN_IMAGE_RE.search(raw)
        or _BOLD_RE.search(raw)
        or _ITALIC_STAR_RE.search(raw)
        or _HEADING_RE.search(raw)
        or _TASK_BULLET_RE.search(raw)
        or _BULLET_RE.search(raw)
        or _NUMBERED_RE.search(raw)
        or _BLOCKQUOTE_RE.search(raw)
        or _TABLE_SEPARATOR_RE.search(raw)
    )


def looks_like_question_reply(text: Any, *, allow_exclamatory_rhetorical: bool = False) -> bool:
    """Detect surface-form questions in final visible text.

    This is an output-style guard only; it does not infer user intent or route
    dialogue semantics.
    """

    raw = re.sub(r"\s+", "", str(text or "").strip())
    if not raw:
        return False
    if allow_exclamatory_rhetorical and "?" not in raw and "？" not in raw:
        clauses = re.findall(r"[^。！？!?]+[。！？!?]*", raw)
        kept: list[str] = []
        for clause in clauses:
            if clause.endswith(("!", "！")) and (
                _QUESTION_TAIL_RE.search(clause) or _QUESTION_FRAGMENT_RE.search(clause)
            ):
                continue
            kept.append(clause)
        raw = "".join(kept)
        if not raw:
            return False
    return bool(
        _QUESTION_MARK_RE.search(raw)
        or _QUESTION_TAIL_RE.search(raw)
        or _QUESTION_FRAGMENT_RE.search(raw)
    )


__all__ = [
    "looks_like_formulaic_reply_tic",
    "looks_like_markdown_reply",
    "looks_like_question_reply",
    "looks_like_visible_reasoning_trace",
    "normalize_visible_reply_text",
]
