from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from nonebot_plugin_personification.core.sticker_library import (
    STICKER_SCHEMA_VERSION,
    STICKER_VISION_PROMPT,
    SUPPORTED_STICKER_SUFFIXES,
    analyze_sticker_image,
    compute_file_hash,
    compute_folder_hash,
    image_file_to_data_url as image_file_to_data_url_core,
    list_local_sticker_files,
    load_sticker_metadata,
    normalize_label_result as normalize_label_result_core,
    resolve_sticker_dir,
    save_sticker_metadata,
)
from nonebot_plugin_personification.skills.skillpacks.vision_caller.scripts.impl import VisionCaller


LABELING_PROMPT = STICKER_VISION_PROMPT
STICKER_SUFFIXES = set(SUPPORTED_STICKER_SUFFIXES)


def _extract_runtime_and_vision_caller(value: Optional[Any]) -> tuple[Any | None, Optional[VisionCaller]]:
    if value is None:
        return None, None
    if hasattr(value, "plugin_config"):
        return value, getattr(value, "vision_caller", None)
    return None, value


@dataclass
class StickerLabeler:
    sticker_dir: Path
    logger: Any
    concurrency: int = 3

    def __post_init__(self) -> None:
        self.sticker_dir = resolve_sticker_dir(self.sticker_dir, create=True)
        self._write_lock = asyncio.Lock()

    @property
    def metadata_path(self) -> Path:
        return self.sticker_dir / "stickers.json"

    @property
    def file_lock_path(self) -> Path:
        return self.sticker_dir / "stickers.json.lock"

    def list_sticker_files(self) -> list[Path]:
        return list_local_sticker_files(self.sticker_dir)

    def load_metadata(self) -> dict:
        return load_sticker_metadata(self.sticker_dir)

    async def save_metadata(self, metadata: dict) -> None:
        async with self._write_lock:
            await save_sticker_metadata(self.sticker_dir, metadata)

    def compute_folder_hash(self, files: Optional[Iterable[Path]] = None) -> str:
        return compute_folder_hash(files or self.list_sticker_files())

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

    async def label_file(self, file: Path, vision_caller: Any) -> dict:
        runtime, effective_vision_caller = _extract_runtime_and_vision_caller(vision_caller)
        payload = file.read_bytes()
        file_hash = compute_file_hash(payload)
        if runtime is not None:
            result = await analyze_sticker_image(
                runtime=runtime,
                image_refs=[image_file_to_data_url_core(file)],
                fallback_vision_caller=effective_vision_caller,
            )
            if result.vision_route in {"vision_unavailable", "missing_images"}:
                raise RuntimeError(result.vision_route)
            if not result.is_sticker:
                return {"is_sticker": False, "file_hash": file_hash}
            return normalize_label_result_core(
                {
                    "description": result.description,
                    "mood_tags": result.mood_tags,
                    "scene_tags": result.scene_tags,
                    "proactive_send": result.proactive_send,
                    "ocr_text": result.ocr_text,
                    "use_hint": result.use_hint,
                    "avoid_hint": result.avoid_hint,
                    "vision_route": result.vision_route,
                },
                model_name="",
                vision_route=result.vision_route,
                file_hash=file_hash,
            )
        raw = await effective_vision_caller.describe(LABELING_PROMPT, image_file_to_data_url_core(file))
        from nonebot_plugin_personification.core.sticker_library import normalize_sticker_vision_result as _nsv
        parsed = _nsv(raw, vision_route="vision_fallback")
        if not parsed.is_sticker:
            return {"is_sticker": False, "file_hash": file_hash}
        return normalize_label_result_core(
            raw,
            model_name=getattr(effective_vision_caller, "model", ""),
            vision_route="vision_fallback",
            file_hash=file_hash,
        )

    async def scan_on_startup(self, vision_caller: Optional[Any]) -> None:
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
        metadata["_meta"]["schema_version"] = STICKER_SCHEMA_VERSION
        await self.save_metadata(metadata)
        elapsed = time.perf_counter() - start
        self.logger.info(f"[sticker labeler] 完成，成功 {success} 张，失败 {failed} 张，耗时 {elapsed:.1f}s")
        if failed_files:
            self.logger.warning(f"[sticker labeler] 失败列表：{failed_files}（可手动重新触发打标）")

    async def relabel(
        self,
        vision_caller: Optional[Any],
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
        metadata["_meta"]["schema_version"] = STICKER_SCHEMA_VERSION
        await self.save_metadata(metadata)
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "matched": [file.name for file in pending],
            "failed_files": failed_files,
        }

    async def handle_created(self, file_path: Path, vision_caller: Optional[Any]) -> None:
        file_path = Path(file_path)
        if file_path.suffix.lower() not in STICKER_SUFFIXES or vision_caller is None or not file_path.exists():
            return
        metadata = self.load_metadata()
        metadata[file_path.name] = await self.label_file(file_path, vision_caller)
        metadata["_meta"]["folder_hash"] = self.compute_folder_hash()
        metadata["_meta"]["schema_version"] = STICKER_SCHEMA_VERSION
        await self.save_metadata(metadata)

    async def handle_deleted(self, file_path: Path) -> None:
        file_path = Path(file_path)
        metadata = self.load_metadata()
        if file_path.name in metadata:
            metadata.pop(file_path.name, None)
            metadata["_meta"]["folder_hash"] = self.compute_folder_hash()
            metadata["_meta"]["schema_version"] = STICKER_SCHEMA_VERSION
            await self.save_metadata(metadata)


def _has_complete_label_fields(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("is_sticker") is False:
        return True
    required = ("description", "mood_tags", "scene_tags", "proactive_send", "ocr_text", "use_hint", "avoid_hint")
    return all(field in value for field in required)


def image_file_to_data_url(file_path: Path) -> str:
    return image_file_to_data_url_core(file_path)


def normalize_label_result(raw: str, model_name: str = "") -> dict:
    return normalize_label_result_core(raw, model_name=model_name, vision_route="vision_fallback")


class _StickerDirEventHandler:
    def __init__(self, labeler: StickerLabeler, vision_caller: Optional[Any], loop: asyncio.AbstractEventLoop):
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
    vision_caller: Optional[Any],
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
    vision_caller: Optional[Any],
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

