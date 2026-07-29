from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal


MediaOrigin = Literal["current", "quoted", "batch"]

_ALLOWED_ORIGINS = {"current", "quoted", "batch"}
_ALLOWED_KINDS = {"image", "sticker", "gif", "mface", "video", "unknown"}
_DATA_URL_RE = re.compile(r"data:[^\s,;]+;base64,[A-Za-z0-9+/=\r\n]+", re.IGNORECASE)
_SUMMARY_MARKER_RE = re.compile(
    r"\[(?:图片视觉描述|表情包语义|动态表情语义|媒体语义)（系统注入[^）]*）[：:]\s*(.*?)\]",
    re.DOTALL,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _segment_type(segment: Any) -> str:
    if isinstance(segment, dict):
        return _text(segment.get("type")).lower()
    return _text(getattr(segment, "type", "")).lower()


def _segment_data(segment: Any) -> dict[str, Any]:
    if isinstance(segment, dict):
        data = segment.get("data", {})
    else:
        data = getattr(segment, "data", {})
    return dict(data) if isinstance(data, dict) else {}


def _message_segments(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict) and "message" in value:
        value = value.get("message")
    if isinstance(value, (str, bytes, bytearray, dict)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _object_value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _sender_user_id(value: Any) -> str:
    sender = _object_value(value, "sender")
    sender_id = _object_value(sender, "user_id") if sender is not None else None
    return _text(sender_id or _object_value(value, "user_id"))


def _message_id(value: Any) -> str:
    return _text(
        _object_value(value, "message_id")
        or _object_value(value, "id")
        or _object_value(value, "messageId")
    )


def _file_id(data: dict[str, Any]) -> str:
    return _text(
        data.get("file_id")
        or data.get("fileId")
        or data.get("file")
        or data.get("id")
        or data.get("emoji_id")
    )


def _media_ref(data: dict[str, Any]) -> str:
    return _text(data.get("url") or data.get("src") or data.get("file"))


def _content_hash(ref: str, file_id: str) -> str:
    raw_ref = _text(ref)
    if raw_ref.lower().startswith("data:") and "," in raw_ref:
        encoded = raw_ref.split(",", 1)[1]
        try:
            payload = base64.b64decode(encoded, validate=False)
        except Exception:
            payload = encoded.encode("utf-8", errors="ignore")
        return hashlib.sha256(payload).hexdigest()
    seed = raw_ref or _text(file_id)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest() if seed else ""


def _kind_for_segment(segment_type: str, data: dict[str, Any]) -> str:
    if segment_type == "mface":
        return "mface"
    if segment_type == "gif":
        return "gif"
    if segment_type == "video":
        return "video"
    if segment_type != "image":
        return "unknown"
    raw_sub_type = data.get("sub_type", data.get("subType", 0))
    try:
        return "sticker" if int(raw_sub_type or 0) == 1 else "image"
    except (TypeError, ValueError):
        return "image"


def normalize_safe_visual_summary(value: Any, *, limit: int = 500) -> str:
    text = _text(value)
    if not text:
        return ""
    match = _SUMMARY_MARKER_RE.search(text)
    if match:
        text = _text(match.group(1))
    text = _DATA_URL_RE.sub("[media-data-omitted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


@dataclass(frozen=True)
class TurnMediaRef:
    media_id: str
    ref: str
    origin: MediaOrigin
    owner_user_id: str
    message_id: str
    kind: str
    content_hash: str = ""
    file_id: str = ""
    safe_summary: str = ""
    confidence: float = 0.0
    summary_scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        ref = _text(self.ref)
        return {
            "media_id": _text(self.media_id),
            "ref": "" if ref.lower().startswith("data:") else ref,
            "origin": self.origin if self.origin in _ALLOWED_ORIGINS else "current",
            "owner_user_id": _text(self.owner_user_id),
            "message_id": _text(self.message_id),
            "kind": self.kind if self.kind in _ALLOWED_KINDS else "unknown",
            "content_hash": _text(self.content_hash),
            "file_id": _text(self.file_id),
            "safe_summary": normalize_safe_visual_summary(self.safe_summary),
            "confidence": max(0.0, min(1.0, float(self.confidence or 0.0))),
            "summary_scope": _text(self.summary_scope),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TurnMediaRef | None:
        if not isinstance(value, dict):
            return None
        origin = _text(value.get("origin")).lower()
        if origin not in _ALLOWED_ORIGINS:
            origin = "current"
        kind = _text(value.get("kind")).lower()
        if kind not in _ALLOWED_KINDS:
            kind = "unknown"
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0) or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        file_id = _text(value.get("file_id"))
        ref = _text(value.get("ref"))
        content_hash = _text(value.get("content_hash")) or _content_hash(ref, file_id)
        media_id = _text(value.get("media_id"))
        if not media_id:
            media_id = _build_media_id(
                owner_user_id=_text(value.get("owner_user_id")),
                message_id=_text(value.get("message_id")),
                origin=origin,
                kind=kind,
                file_id=file_id,
                content_hash=content_hash,
            )
        return cls(
            media_id=media_id,
            ref=ref,
            origin=origin,  # type: ignore[arg-type]
            owner_user_id=_text(value.get("owner_user_id")),
            message_id=_text(value.get("message_id")),
            kind=kind,
            content_hash=content_hash,
            file_id=file_id,
            safe_summary=normalize_safe_visual_summary(value.get("safe_summary")),
            confidence=confidence,
            summary_scope=_text(value.get("summary_scope")),
        )


def _build_media_id(
    *,
    owner_user_id: str,
    message_id: str,
    origin: str,
    kind: str,
    file_id: str,
    content_hash: str,
) -> str:
    seed = "\0".join(
        (owner_user_id, message_id, origin, kind, file_id, content_hash)
    )
    return f"media_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def extract_media_from_message(
    message: Any,
    *,
    origin: MediaOrigin,
    owner_user_id: str,
    message_id: str,
) -> list[TurnMediaRef]:
    refs: list[TurnMediaRef] = []
    for segment in _message_segments(message):
        segment_type = _segment_type(segment)
        if segment_type not in {"image", "mface", "gif", "video"}:
            continue
        data = _segment_data(segment)
        ref = _media_ref(data)
        file_id = _file_id(data)
        if not ref and not file_id:
            continue
        kind = _kind_for_segment(segment_type, data)
        content_hash = _content_hash(ref, file_id)
        refs.append(
            TurnMediaRef(
                media_id=_build_media_id(
                    owner_user_id=_text(owner_user_id),
                    message_id=_text(message_id),
                    origin=origin,
                    kind=kind,
                    file_id=file_id,
                    content_hash=content_hash,
                ),
                ref=ref,
                origin=origin,
                owner_user_id=_text(owner_user_id),
                message_id=_text(message_id),
                kind=kind,
                content_hash=content_hash,
                file_id=file_id,
            )
        )
    return refs


def extract_turn_media_from_event(
    event: Any,
    *,
    current_origin: MediaOrigin = "current",
    include_quoted: bool = True,
) -> list[TurnMediaRef]:
    refs = extract_media_from_message(
        _object_value(event, "message"),
        origin=current_origin,
        owner_user_id=_sender_user_id(event),
        message_id=_message_id(event),
    )
    if not include_quoted:
        return refs
    quoted = _object_value(event, "reply") or _object_value(event, "quoted") or _object_value(event, "quote")
    if quoted:
        refs.extend(
            extract_media_from_message(
                _object_value(quoted, "message"),
                origin="quoted",
                owner_user_id=_sender_user_id(quoted),
                message_id=_message_id(quoted),
            )
        )
    return refs


def serialize_turn_media(values: Iterable[TurnMediaRef | dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values or []:
        item = value if isinstance(value, TurnMediaRef) else TurnMediaRef.from_dict(value)
        if item is None or item.media_id in seen:
            continue
        seen.add(item.media_id)
        serialized.append(item.to_dict())
    return serialized


def coerce_turn_media(values: Iterable[TurnMediaRef | dict[str, Any]] | None) -> list[TurnMediaRef]:
    refs: list[TurnMediaRef] = []
    seen: set[str] = set()
    for value in values or []:
        item = value if isinstance(value, TurnMediaRef) else TurnMediaRef.from_dict(value)
        if item is None or item.media_id in seen:
            continue
        seen.add(item.media_id)
        refs.append(item)
    return refs


def media_from_batched_events(values: Iterable[dict[str, Any]] | None) -> list[TurnMediaRef]:
    refs: list[TurnMediaRef] = []
    for value in values or []:
        if isinstance(value, dict):
            refs.extend(coerce_turn_media(value.get("media") or []))
    return coerce_turn_media(refs)


def attach_safe_visual_summary(
    values: Iterable[TurnMediaRef | dict[str, Any]],
    summary: Any,
    *,
    confidence: float = 0.65,
) -> list[TurnMediaRef]:
    refs = coerce_turn_media(values)
    safe_summary = normalize_safe_visual_summary(summary)
    if not safe_summary:
        return refs
    scope = "single_media" if len(refs) == 1 else "turn_aggregate"
    return [
        replace(
            item,
            safe_summary=safe_summary,
            confidence=max(0.0, min(1.0, float(confidence or 0.0))),
            summary_scope=scope,
        )
        for item in refs
    ]


def render_turn_media_grounding(
    values: Iterable[TurnMediaRef | dict[str, Any]] | None,
    *,
    summary: Any = "",
) -> str:
    refs = coerce_turn_media(values)
    safe_summary = normalize_safe_visual_summary(summary)
    if not safe_summary:
        safe_summary = next((item.safe_summary for item in refs if item.safe_summary), "")
    if not refs and not safe_summary:
        return ""
    lines = [
        "## 聊天媒体 provenance 与视觉 grounding（系统事实）",
        "- 每个媒体只归属于下列 owner/message/origin；不要把 batch 或 quoted 图片归给当前触发者。",
        "- 画中主体只是媒体内容，不是聊天参与者。除非聊天文本或协议事实另有明确证据，不得把画中人物认作发送者、群友或 bot。",
        "- 多人构图、视线、站位、拥挤感或戏剧情绪只说明画面表现，不证明群友在现实中围观、施压、注视或参与该场景。",
    ]
    for item in refs[:12]:
        identity = item.file_id or item.content_hash[:16] or "unknown"
        lines.append(
            f"- {item.media_id}: origin={item.origin}; owner_user_id={item.owner_user_id or 'unknown'}; "
            f"message_id={item.message_id or 'unknown'}; kind={item.kind}; identity={identity}"
        )
    if safe_summary:
        scope = "single_media" if len(refs) == 1 else "turn_aggregate_do_not_split_by_person"
        summary_confidence = max((item.confidence for item in refs), default=0.65)
        lines.append(
            f"- 安全视觉摘要（scope={scope}，confidence={summary_confidence:.2f}）：{safe_summary}"
        )
    else:
        lines.append("- 安全视觉摘要不可用：只能使用 provenance 和聊天文字，不得补猜画面内容。")
    return "\n".join(lines)


def media_summary_timeout_seconds(
    response_deadline: float | None,
    *,
    now: float,
    maximum: float = 18.0,
    reserve: float = 30.0,
) -> float:
    if response_deadline is None:
        return max(0.0, float(maximum))
    remaining = float(response_deadline) - float(now) - max(0.0, float(reserve))
    return max(0.0, min(float(maximum), remaining))


__all__ = [
    "MediaOrigin",
    "TurnMediaRef",
    "attach_safe_visual_summary",
    "coerce_turn_media",
    "extract_media_from_message",
    "extract_turn_media_from_event",
    "media_from_batched_events",
    "media_summary_timeout_seconds",
    "normalize_safe_visual_summary",
    "render_turn_media_grounding",
    "serialize_turn_media",
]
