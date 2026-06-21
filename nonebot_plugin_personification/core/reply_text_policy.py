from __future__ import annotations

import re
from typing import Any


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
        _FENCED_CODE_RE.search(raw)
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


__all__ = [
    "looks_like_markdown_reply",
    "normalize_visible_reply_text",
]
