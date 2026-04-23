from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from filelock import FileLock

from .media_understanding import analyze_images_with_route_or_fallback
from .sticker_semantics import ALLOWED_STICKER_MOOD_TAGS, ALLOWED_STICKER_SCENE_TAGS


DEFAULT_STICKER_DIR = Path("data/stickers")
STICKER_SCHEMA_VERSION = 3
SUPPORTED_STICKER_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
RESOLVABLE_STICKER_SUFFIXES = SUPPORTED_STICKER_SUFFIXES | {".gif"}
ALLOWED_MOOD_TAGS = set(ALLOWED_STICKER_MOOD_TAGS)
ALLOWED_SCENE_TAGS = set(ALLOWED_STICKER_SCENE_TAGS)

STICKER_VISION_PROMPT = """你是表情包语义分析器。请完整理解这张静态表情包或截图，并严格返回 JSON。
不要输出 markdown，不要输出解释，只返回一个 JSON 对象。

{
  "summary": "25字内概括这张图最核心的意思",
  "description": "20-60字，说明主体、动作、表情、画面关系和文字信息",
  "ocr_text": "图中文字，没有就填空字符串",
  "use_hint": "适合在什么场景发，15-40字",
  "avoid_hint": "明显不适合在哪些场景发，15-40字",
  "mood_tags": ["情绪标签"],
  "scene_tags": ["场景标签"],
  "proactive_send": true,
  "should_collect": true,
  "collect_reason": "为什么值得收藏，15字内",
  "is_sticker": true
}

要求：
1. 必须结合画面主体、动作/表情、人物关系、图中文字一起理解。
2. mood_tags 只允许从以下标签里选 1-4 个：搞笑、开心、感动、尴尬、无语、惊讶、委屈、生气、害羞、得意、困惑、赞同、拒绝、期待、失落、撒娇、淡定、震惊。
3. scene_tags 只允许从以下标签里选 1-4 个：回应笑点、接梗、表达赞同、化解尴尬、自嘲、反驳、表达惊讶、安慰对方、撒娇、表示无奈、冷场时、表达期待、庆祝、拒绝请求、结束对话、打招呼、表达关心、吐槽、卖萌、表达疑惑。
4. should_collect 为 true 只在这张图语义明确、可复用、不是普通照片时给出。
5. 如果内容不清晰，也要保守填写，并把 should_collect 设为 false。
6. is_sticker 为 false 当且仅当这张图是截图、实拍照片、UI界面截图等非表情包内容；卡通、二次元、meme、梗图等创作图给 true。"""


@dataclass(frozen=True)
class StickerVisionResult:
    summary: str
    description: str
    ocr_text: str
    use_hint: str
    avoid_hint: str
    mood_tags: list[str]
    scene_tags: list[str]
    proactive_send: bool
    should_collect: bool
    collect_reason: str
    is_sticker: bool = True
    vision_route: str = ""


def resolve_sticker_dir(raw_path: str | Path | None, *, create: bool = False) -> Path:
    text = str(raw_path or "").strip().strip('"').strip("'")
    path = Path(text).expanduser() if text else DEFAULT_STICKER_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def list_local_sticker_files(sticker_dir: str | Path | None, *, include_gif: bool = False) -> list[Path]:
    base_dir = resolve_sticker_dir(sticker_dir)
    if not base_dir.exists() or not base_dir.is_dir():
        return []
    allowed_suffixes = RESOLVABLE_STICKER_SUFFIXES if include_gif else SUPPORTED_STICKER_SUFFIXES
    return sorted(
        file
        for file in base_dir.iterdir()
        if file.is_file() and file.suffix.lower() in allowed_suffixes
    )


def sticker_metadata_path(sticker_dir: str | Path | None) -> Path:
    return resolve_sticker_dir(sticker_dir) / "stickers.json"


def sticker_metadata_lock_path(sticker_dir: str | Path | None) -> Path:
    return resolve_sticker_dir(sticker_dir) / "stickers.json.lock"


def compute_file_hash(payload: bytes) -> str:
    return hashlib.sha256(bytes(payload or b"")).hexdigest()


def compute_folder_hash(files: Iterable[Path]) -> str:
    hasher = hashlib.sha256()
    for file in files:
        stat = file.stat()
        hasher.update(file.name.encode("utf-8"))
        hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
        hasher.update(str(stat.st_size).encode("utf-8"))
    return hasher.hexdigest()


def image_file_to_data_url(file_path: Path) -> str:
    mime_type = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def image_bytes_to_data_url(payload: bytes, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(bytes(payload or b"")).decode("ascii")
    safe_mime = str(mime_type or "").strip() or "image/jpeg"
    return f"data:{safe_mime};base64,{encoded}"


def normalize_sticker_entry(
    value: Any,
    *,
    file_name: str = "",
    model_name: str = "",
    vision_route: str = "",
    file_hash: str = "",
    source_kind: str = "",
    source_group_id: str = "",
    source_user_id: str = "",
    collected_at: str = "",
) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"description": value}
    if not isinstance(value, dict):
        value = {}

    mood_tags = [
        str(tag).strip()
        for tag in list(value.get("mood_tags") or [])
        if str(tag).strip() in ALLOWED_MOOD_TAGS
    ][:4]
    scene_tags = [
        str(tag).strip()
        for tag in list(value.get("scene_tags") or [])
        if str(tag).strip() in ALLOWED_SCENE_TAGS
    ][:4]
    raw_weight = value.get("weight", 1.0)
    try:
        weight = max(0.0, min(3.0, float(raw_weight if raw_weight is not None else 1.0)))
    except (TypeError, ValueError):
        weight = 1.0
    entry = {
        "description": str(value.get("description", "") or file_name or "图片内容不清晰").strip() or "图片内容不清晰",
        "mood_tags": mood_tags or ["淡定"],
        "scene_tags": scene_tags or ["表达疑惑"],
        "proactive_send": bool(value.get("proactive_send", False)),
        "ocr_text": str(value.get("ocr_text", "") or "").strip(),
        "use_hint": str(value.get("use_hint", "") or "").strip(),
        "avoid_hint": str(value.get("avoid_hint", "") or "").strip(),
        "weight": round(weight, 2),
        "file_hash": str(value.get("file_hash", "") or file_hash).strip(),
        "collected_at": str(value.get("collected_at", "") or collected_at).strip(),
        "source_kind": str(value.get("source_kind", "") or source_kind).strip(),
        "source_group_id": str(value.get("source_group_id", "") or source_group_id).strip(),
        "source_user_id": str(value.get("source_user_id", "") or source_user_id).strip(),
        "vision_route": str(value.get("vision_route", "") or vision_route).strip(),
        "labeled_at": str(value.get("labeled_at", "") or time.strftime("%Y-%m-%d %H:%M")).strip(),
        "model": str(value.get("model", "") or model_name).strip(),
    }
    return entry


def normalize_sticker_metadata(data: Any, *, files: Iterable[Path] | None = None) -> dict[str, Any]:
    normalized_files = list(files or [])
    existing_names = {file.name for file in normalized_files}
    loaded = data if isinstance(data, dict) else {}
    metadata: dict[str, Any] = {}
    for key, value in loaded.items():
        if key == "_meta":
            continue
        file_name = str(key or "").strip()
        if not file_name:
            continue
        if existing_names and file_name not in existing_names:
            continue
        metadata[file_name] = normalize_sticker_entry(value, file_name=Path(file_name).stem)

    for file in normalized_files:
        metadata.setdefault(file.name, normalize_sticker_entry({}, file_name=file.stem))

    meta_value = loaded.get("_meta", {}) if isinstance(loaded, dict) else {}
    folder_hash = str(meta_value.get("folder_hash", "") or "").strip()
    metadata["_meta"] = {
        "folder_hash": folder_hash or compute_folder_hash(normalized_files),
        "schema_version": STICKER_SCHEMA_VERSION,
    }
    return metadata


def load_sticker_metadata(sticker_dir: str | Path | None) -> dict[str, Any]:
    base_dir = resolve_sticker_dir(sticker_dir)
    files = list_local_sticker_files(base_dir)
    path = sticker_metadata_path(base_dir)
    if not path.exists():
        return normalize_sticker_metadata({}, files=files)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    return normalize_sticker_metadata(loaded, files=files)


def save_sticker_metadata_sync(sticker_dir: str | Path | None, metadata: dict[str, Any]) -> None:
    base_dir = resolve_sticker_dir(sticker_dir, create=True)
    files = list_local_sticker_files(base_dir)
    normalized = normalize_sticker_metadata(metadata, files=files)
    normalized["_meta"]["folder_hash"] = compute_folder_hash(files)
    path = sticker_metadata_path(base_dir)
    lock_path = sticker_metadata_lock_path(base_dir)
    with FileLock(str(lock_path)):
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")


async def save_sticker_metadata(sticker_dir: str | Path | None, metadata: dict[str, Any]) -> None:
    await asyncio.to_thread(save_sticker_metadata_sync, sticker_dir, metadata)


def find_sticker_by_hash(metadata: dict[str, Any], file_hash: str) -> str:
    target = str(file_hash or "").strip()
    if not target:
        return ""
    for file_name, entry in (metadata or {}).items():
        if file_name == "_meta" or not isinstance(entry, dict):
            continue
        if str(entry.get("file_hash", "") or "").strip() == target:
            return file_name
    return ""


def _safe_stem(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(text or "").strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:40] or "sticker"


def save_collected_sticker_sync(
    sticker_dir: str | Path | None,
    *,
    payload: bytes,
    mime_type: str,
    file_name_hint: str = "",
) -> tuple[Path, bool, str]:
    base_dir = resolve_sticker_dir(sticker_dir, create=True)
    metadata = load_sticker_metadata(base_dir)
    file_hash = compute_file_hash(payload)
    existing_name = find_sticker_by_hash(metadata, file_hash)
    if existing_name:
        return base_dir / existing_name, False, file_hash

    suffix = mimetypes.guess_extension(str(mime_type or "").split(";")[0].strip()) or ".png"
    suffix = suffix.lower()
    if suffix not in SUPPORTED_STICKER_SUFFIXES:
        suffix = ".png"
    file_name = f"{_safe_stem(file_name_hint)}_{file_hash[:12]}{suffix}"
    target = base_dir / file_name
    target.write_bytes(bytes(payload or b""))
    return target, True, file_hash


async def save_collected_sticker(
    sticker_dir: str | Path | None,
    *,
    payload: bytes,
    mime_type: str,
    file_name_hint: str = "",
) -> tuple[Path, bool, str]:
    return await asyncio.to_thread(
        save_collected_sticker_sync,
        sticker_dir,
        payload=payload,
        mime_type=mime_type,
        file_name_hint=file_name_hint,
    )


def _parse_json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return bool(default)


def normalize_sticker_vision_result(raw: Any, *, vision_route: str = "") -> StickerVisionResult:
    data = _parse_json_payload(raw)
    summary = str(data.get("summary", "") or "").strip()
    description = str(data.get("description", "") or summary or "图片内容不清晰").strip() or "图片内容不清晰"
    ocr_text = str(data.get("ocr_text", "") or "").strip()
    use_hint = str(data.get("use_hint", "") or "").strip()
    avoid_hint = str(data.get("avoid_hint", "") or "").strip()
    mood_tags = [
        str(tag).strip()
        for tag in list(data.get("mood_tags") or [])
        if str(tag).strip() in ALLOWED_MOOD_TAGS
    ][:4]
    scene_tags = [
        str(tag).strip()
        for tag in list(data.get("scene_tags") or [])
        if str(tag).strip() in ALLOWED_SCENE_TAGS
    ][:4]
    if not summary:
        summary = description[:40]
    return StickerVisionResult(
        summary=summary,
        description=description,
        ocr_text=ocr_text,
        use_hint=use_hint,
        avoid_hint=avoid_hint,
        mood_tags=mood_tags or ["淡定"],
        scene_tags=scene_tags or ["表达疑惑"],
        proactive_send=_normalize_bool(data.get("proactive_send"), False),
        should_collect=_normalize_bool(data.get("should_collect"), False),
        collect_reason=str(data.get("collect_reason", "") or "").strip(),
        is_sticker=_normalize_bool(data.get("is_sticker"), True),
        vision_route=str(vision_route or data.get("vision_route", "") or "").strip(),
    )


async def analyze_sticker_image(
    *,
    runtime: Any,
    image_refs: Sequence[str],
    prompt: str = STICKER_VISION_PROMPT,
    fallback_vision_caller: Any = None,
) -> StickerVisionResult:
    raw, route = await analyze_images_with_route_or_fallback(
        runtime=runtime,
        prompt=prompt,
        image_refs=image_refs,
        fallback_vision_caller=fallback_vision_caller,
    )
    return normalize_sticker_vision_result(raw, vision_route=route)


def render_sticker_semantic_summary(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return ""
    description = str(entry.get("description", "") or "").strip()
    ocr_text = str(entry.get("ocr_text", "") or "").strip()
    use_hint = str(entry.get("use_hint", "") or "").strip()
    avoid_hint = str(entry.get("avoid_hint", "") or "").strip()
    mood_tags = "、".join(str(tag) for tag in (entry.get("mood_tags") or [])[:4] if str(tag).strip())
    scene_tags = "、".join(str(tag) for tag in (entry.get("scene_tags") or [])[:4] if str(tag).strip())
    parts: list[str] = []
    if description:
        parts.append(f"主体与动作：{description}")
    if ocr_text:
        parts.append(f"图中文字：{ocr_text}")
    if use_hint:
        parts.append(f"适用：{use_hint}")
    if avoid_hint:
        parts.append(f"不适用：{avoid_hint}")
    if mood_tags:
        parts.append(f"情绪标签：{mood_tags}")
    if scene_tags:
        parts.append(f"场景标签：{scene_tags}")
    return "；".join(parts)


def normalize_label_result(
    raw: str,
    *,
    model_name: str = "",
    vision_route: str = "",
    file_hash: str = "",
    source_kind: str = "",
    source_group_id: str = "",
    source_user_id: str = "",
    collected_at: str = "",
) -> dict[str, Any]:
    normalized = normalize_sticker_vision_result(raw, vision_route=vision_route)
    return normalize_sticker_entry(
        {
            "description": normalized.description,
            "mood_tags": normalized.mood_tags,
            "scene_tags": normalized.scene_tags,
            "proactive_send": normalized.proactive_send,
            "ocr_text": normalized.ocr_text,
            "use_hint": normalized.use_hint,
            "avoid_hint": normalized.avoid_hint,
        },
        model_name=model_name,
        vision_route=normalized.vision_route,
        file_hash=file_hash,
        source_kind=source_kind,
        source_group_id=source_group_id,
        source_user_id=source_user_id,
        collected_at=collected_at,
    )


__all__ = [
    "DEFAULT_STICKER_DIR",
    "RESOLVABLE_STICKER_SUFFIXES",
    "STICKER_SCHEMA_VERSION",
    "STICKER_VISION_PROMPT",
    "SUPPORTED_STICKER_SUFFIXES",
    "StickerVisionResult",
    "analyze_sticker_image",
    "compute_file_hash",
    "compute_folder_hash",
    "find_sticker_by_hash",
    "image_bytes_to_data_url",
    "image_file_to_data_url",
    "list_local_sticker_files",
    "load_sticker_metadata",
    "normalize_label_result",
    "normalize_sticker_entry",
    "normalize_sticker_metadata",
    "normalize_sticker_vision_result",
    "render_sticker_semantic_summary",
    "resolve_sticker_dir",
    "save_collected_sticker",
    "save_collected_sticker_sync",
    "save_sticker_metadata",
    "save_sticker_metadata_sync",
    "sticker_metadata_path",
]
