from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from filelock import FileLock

from nonebot_plugin_personification.skills.skillpacks.vision_caller.scripts.impl import VisionCaller


LABELING_PROMPT = """你是一个表情包分析助手。请分析这张表情包图片，严格按照以下 JSON 格式返回结果。
不要输出任何其他内容，不要用 markdown 代码块包裹，直接输出 JSON。

{
  "description": "图片内容描述",
  "mood_tags": ["情绪标签"],
  "scene_tags": ["场景标签"],
  "proactive_send": true或false
}

各字段要求：

description（必填）：
- 20-40字中文，一句话
- 描述：角色是什么/在做什么/表情如何/图上有什么文字
- 结尾说明：适合什么情况下使用
- 不要以"这是"开头，直接描述
- 好的示例："猫咪双手捂脸，表情崩溃，旁边写着'我不听我不听'，适合表达不想面对现实时使用"

mood_tags（必填，2-4个）：
只从以下列表选择，不要自创标签：
搞笑、开心、感动、尴尬、无语、惊讶、委屈、生气、害羞、得意、困惑、赞同、拒绝、期待、失落、撒娇、淡定、震惊

scene_tags（必填，2-4个）：
只从以下列表选择，不要自创标签：
回应笑点、接梗、表达赞同、化解尴尬、自嘲、反驳、表达惊讶、安慰对方、
撒娇、表示无奈、冷场时、表达期待、庆祝、拒绝请求、结束对话、
打招呼、表达关心、吐槽、卖萌、表达疑惑

proactive_send（必填）：
- true：这张图可以主动发出，不需要对方先说什么（如：打招呼用、表达心情用、卖萌用）
- false：这张图只适合回应特定内容（如：专门用来回应笑话、专门用来反驳某个观点）

如果图片内容不清晰或无法判断，description 填"图片内容不清晰"，其余字段给出最保守的猜测。"""

ALLOWED_MOOD_TAGS = {
    "搞笑", "开心", "感动", "尴尬", "无语", "惊讶", "委屈", "生气", "害羞",
    "得意", "困惑", "赞同", "拒绝", "期待", "失落", "撒娇", "淡定", "震惊",
}
ALLOWED_SCENE_TAGS = {
    "回应笑点", "接梗", "表达赞同", "化解尴尬", "自嘲", "反驳", "表达惊讶", "安慰对方",
    "撒娇", "表示无奈", "冷场时", "表达期待", "庆祝", "拒绝请求", "结束对话",
    "打招呼", "表达关心", "吐槽", "卖萌", "表达疑惑",
}
STICKER_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass
class StickerLabeler:
    sticker_dir: Path
    logger: Any
    concurrency: int = 3

    def __post_init__(self) -> None:
        self.sticker_dir = Path(self.sticker_dir)
        self._write_lock = asyncio.Lock()

    @property
    def metadata_path(self) -> Path:
        return self.sticker_dir / "stickers.json"

    @property
    def file_lock_path(self) -> Path:
        return self.sticker_dir / "stickers.json.lock"

    def list_sticker_files(self) -> list[Path]:
        if not self.sticker_dir.exists() or not self.sticker_dir.is_dir():
            return []
        return sorted(
            file
            for file in self.sticker_dir.iterdir()
            if file.is_file() and file.suffix.lower() in STICKER_SUFFIXES
        )

    def load_metadata(self) -> dict:
        if not self.metadata_path.exists():
            return {"_meta": {"folder_hash": "", "schema_version": 2}}
        try:
            loaded = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return {"_meta": {"folder_hash": "", "schema_version": 2}}
        if not isinstance(loaded, dict):
            return {"_meta": {"folder_hash": "", "schema_version": 2}}
        loaded.setdefault("_meta", {})
        loaded["_meta"].setdefault("folder_hash", "")
        loaded["_meta"]["schema_version"] = 2
        return loaded

    async def save_metadata(self, metadata: dict) -> None:
        metadata = dict(metadata)
        metadata.setdefault("_meta", {})
        metadata["_meta"]["schema_version"] = 2

        async with self._write_lock:
            def _save() -> None:
                self.sticker_dir.mkdir(parents=True, exist_ok=True)
                with FileLock(str(self.file_lock_path)):
                    self.metadata_path.write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

            await asyncio.to_thread(_save)

    def compute_folder_hash(self, files: Optional[Iterable[Path]] = None) -> str:
        hasher = hashlib.sha256()
        for file in files or self.list_sticker_files():
            stat = file.stat()
            hasher.update(file.name.encode("utf-8"))
            hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
            hasher.update(str(stat.st_size).encode("utf-8"))
        return hasher.hexdigest()

    async def remove_missing_entries(self) -> None:
        metadata = self.load_metadata()
        existing = {file.name for file in self.list_sticker_files()}
        for name in list(metadata.keys()):
            if name == "_meta":
                continue
            if name not in existing:
                metadata.pop(name, None)
        metadata["_meta"]["folder_hash"] = self.compute_folder_hash()
        await self.save_metadata(metadata)

    async def legacy_scan(self) -> None:
        metadata = self.load_metadata()
        files = self.list_sticker_files()
        for file in files:
            if file.name in metadata and isinstance(metadata[file.name], dict):
                metadata[file.name].setdefault("description", file.stem)
                continue
            metadata[file.name] = {
                "description": file.stem,
            }
        for key in list(metadata.keys()):
            if key != "_meta" and key not in {file.name for file in files}:
                metadata.pop(key, None)
        metadata["_meta"]["folder_hash"] = self.compute_folder_hash(files)
        await self.save_metadata(metadata)

    async def label_file(self, file: Path, vision_caller: VisionCaller) -> dict:
        raw = await vision_caller.describe(LABELING_PROMPT, image_file_to_data_url(file))
        return normalize_label_result(raw, model_name=getattr(vision_caller, "model", ""))

    async def scan_on_startup(self, vision_caller: Optional[VisionCaller]) -> None:
        if vision_caller is None:
            return

        files = self.list_sticker_files()
        metadata = self.load_metadata()
        current_hash = self.compute_folder_hash(files)
        if metadata.get("_meta", {}).get("folder_hash") == current_hash:
            return

        pending = [
            file
            for file in files
            if file.name not in metadata or not _has_complete_label_fields(metadata.get(file.name))
        ]
        for key in list(metadata.keys()):
            if key != "_meta" and key not in {file.name for file in files}:
                metadata.pop(key, None)

        total = len(pending)
        self.logger.info(f"[sticker labeler] 开始打标，共 {total} 张新图片，并发数 {max(1, self.concurrency)}")
        start = time.perf_counter()
        success = 0
        failed = 0
        failed_files: list[str] = []
        semaphore = asyncio.Semaphore(max(1, self.concurrency))

        async def _label_one(idx: int, file: Path) -> None:
            nonlocal success, failed
            async with semaphore:
                try:
                    entry = await self.label_file(file, vision_caller)
                    metadata[file.name] = entry
                    success += 1
                    self.logger.info(
                        f"[sticker labeler] [{idx}/{total}] {file.name}  "
                        f"{entry.get('mood_tags', [])} / {entry.get('scene_tags', [])}"
                    )
                except Exception:
                    failed += 1
                    failed_files.append(file.name)

        await asyncio.gather(*[_label_one(idx, file) for idx, file in enumerate(pending, start=1)])
        metadata["_meta"]["folder_hash"] = current_hash
        await self.save_metadata(metadata)
        elapsed = time.perf_counter() - start
        self.logger.info(f"[sticker labeler] 完成，成功 {success} 张，失败 {failed} 张，耗时 {elapsed:.1f}s")
        if failed_files:
            self.logger.warning(f"[sticker labeler] 失败列表：{failed_files}（可手动重新触发打标）")

    async def relabel(
        self,
        vision_caller: Optional[VisionCaller],
        *,
        force: bool = True,
        keyword: str = "",
    ) -> dict[str, Any]:
        if vision_caller is None:
            return {"total": 0, "success": 0, "failed": 0, "matched": []}

        metadata = self.load_metadata()
        files = self.list_sticker_files()
        current_hash = self.compute_folder_hash(files)
        keyword_text = str(keyword or "").strip().lower()
        pending: list[Path] = []
        for file in files:
            if keyword_text and keyword_text not in file.stem.lower() and keyword_text not in file.name.lower():
                continue
            if not force and _has_complete_label_fields(metadata.get(file.name)):
                continue
            pending.append(file)

        total = len(pending)
        success = 0
        failed = 0
        failed_files: list[str] = []
        semaphore = asyncio.Semaphore(max(1, self.concurrency))

        async def _label_one(idx: int, file: Path) -> None:
            nonlocal success, failed
            async with semaphore:
                try:
                    entry = await self.label_file(file, vision_caller)
                    metadata[file.name] = entry
                    success += 1
                    self.logger.info(
                        f"[sticker labeler] [relabel {idx}/{total}] {file.name} "
                        f"{entry.get('mood_tags', [])} / {entry.get('scene_tags', [])}"
                    )
                except Exception as e:
                    failed += 1
                    failed_files.append(file.name)
                    self.logger.warning(f"[sticker labeler] 重打标失败 {file.name}: {e}")

        await asyncio.gather(*[_label_one(idx, file) for idx, file in enumerate(pending, start=1)])
        metadata["_meta"]["folder_hash"] = current_hash
        await self.save_metadata(metadata)
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "matched": [file.name for file in pending],
            "failed_files": failed_files,
        }

    async def handle_created(self, file_path: Path, vision_caller: Optional[VisionCaller]) -> None:
        file_path = Path(file_path)
        if file_path.suffix.lower() not in STICKER_SUFFIXES or vision_caller is None or not file_path.exists():
            return
        metadata = self.load_metadata()
        metadata[file_path.name] = await self.label_file(file_path, vision_caller)
        metadata["_meta"]["folder_hash"] = self.compute_folder_hash()
        await self.save_metadata(metadata)

    async def handle_deleted(self, file_path: Path) -> None:
        file_path = Path(file_path)
        metadata = self.load_metadata()
        if file_path.name in metadata:
            metadata.pop(file_path.name, None)
            metadata["_meta"]["folder_hash"] = self.compute_folder_hash()
            await self.save_metadata(metadata)


def _has_complete_label_fields(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(field in value for field in ("description", "mood_tags", "scene_tags", "proactive_send"))


def image_file_to_data_url(file_path: Path) -> str:
    import base64

    mime_type = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def normalize_label_result(raw: str, model_name: str = "") -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("label result must be a JSON object")

    mood_tags = [tag for tag in data.get("mood_tags", []) if tag in ALLOWED_MOOD_TAGS][:4]
    scene_tags = [tag for tag in data.get("scene_tags", []) if tag in ALLOWED_SCENE_TAGS][:4]
    entry = {
        "description": str(data.get("description", "") or "图片内容不清晰"),
        "mood_tags": mood_tags or ["淡定"],
        "scene_tags": scene_tags or ["表达疑惑"],
        "proactive_send": bool(data.get("proactive_send", False)),
        "labeled_at": time.strftime("%Y-%m-%d %H:%M"),
        "model": model_name or "",
    }
    return entry


class _StickerDirEventHandler:
    def __init__(self, labeler: StickerLabeler, vision_caller: Optional[VisionCaller], loop: asyncio.AbstractEventLoop):
        self.labeler = labeler
        self.vision_caller = vision_caller
        self.loop = loop

    def on_created(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        self.loop.call_soon_threadsafe(
            asyncio.create_task,
            self.labeler.handle_created(Path(event.src_path), self.vision_caller),
        )

    def on_deleted(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        self.loop.call_soon_threadsafe(
            asyncio.create_task,
            self.labeler.handle_deleted(Path(event.src_path)),
        )

    def on_moved(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        self.on_deleted(SimpleEvent(src_path=event.src_path))
        self.on_created(SimpleEvent(src_path=event.dest_path))


@dataclass
class SimpleEvent:
    src_path: str
    is_directory: bool = False


async def scan_on_startup(
    sticker_dir: Path,
    vision_caller: Optional[VisionCaller],
    concurrency: int,
    logger: Any,
) -> StickerLabeler:
    labeler = StickerLabeler(Path(sticker_dir), logger=logger, concurrency=concurrency)
    if vision_caller is None:
        return labeler
    await labeler.scan_on_startup(vision_caller)
    return labeler


def start_watchdog(
    sticker_dir: Path,
    vision_caller: Optional[VisionCaller],
    logger: Any,
    concurrency: int = 3,
    loop: Optional[asyncio.AbstractEventLoop] = None,
):
    if vision_caller is None:
        return None

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    labeler = StickerLabeler(Path(sticker_dir), logger=logger, concurrency=concurrency)
    active_loop = loop or asyncio.get_event_loop()
    adapter = _StickerDirEventHandler(labeler, vision_caller, active_loop)

    class Handler(FileSystemEventHandler):
        def on_created(self, event):  # type: ignore[override]
            adapter.on_created(event)

        def on_deleted(self, event):  # type: ignore[override]
            adapter.on_deleted(event)

        def on_moved(self, event):  # type: ignore[override]
            adapter.on_moved(event)

    observer = Observer()
    Path(sticker_dir).mkdir(parents=True, exist_ok=True)
    observer.schedule(Handler(), str(sticker_dir), recursive=False)
    observer.start()
    return observer

