from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import json

from ...core import webui_audit_log
from ...core.sticker_library import (
    compute_file_hash,
    list_local_sticker_files,
    load_sticker_metadata,
    normalize_sticker_entry,
    resolve_sticker_dir,
    save_sticker_metadata_sync,
    sticker_metadata_path,
)


def _load_raw_manifest(sticker_dir: Path) -> dict[str, Any]:
    """读 stickers.json 原文，跳过 normalize 的兜底逻辑。
    用于判断条目是否真的有用户/labeler 写过 description。
    """
    path = sticker_metadata_path(sticker_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
from ..deps import AdminIdentity, require_admin


_ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
})
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024


def _multipart_available() -> bool:
    """FastAPI 的 File/Form 依赖 python-multipart；缺失时不能注册 upload 端点，
    否则 startup 会失败。这里探测一次，缺包则降级跳过上传功能（其他端点照常）。
    """
    try:
        import python_multipart  # noqa: F401
        return True
    except Exception:
        try:
            import multipart  # noqa: F401  兼容旧版 python-multipart 包名
            return True
        except Exception:
            return False


def _sticker_dir(runtime) -> Path:
    cfg = getattr(runtime, "plugin_config", None)
    raw_path = getattr(cfg, "personification_sticker_path", None) or "data/stickers"
    return resolve_sticker_dir(raw_path, create=True)


def _resolve_safe_file(name: str, sticker_dir: Path) -> Path:
    """防路径穿越；返回绝对路径，若不在 sticker_dir 范围内抛 400。"""
    raw = str(name or "").strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise HTTPException(status_code=400, detail="文件名不合法")
    candidate = (sticker_dir / raw).resolve()
    sticker_root = sticker_dir.resolve()
    try:
        candidate.relative_to(sticker_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="文件路径越界")
    return candidate


def build_sticker_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/stickers", tags=["stickers"])

    @router.get("")
    async def list_stickers(_: AdminIdentity = Depends(require_admin)) -> dict:
        sticker_dir = _sticker_dir(runtime)
        metadata = load_sticker_metadata(sticker_dir)
        raw_manifest = _load_raw_manifest(sticker_dir)
        files = list_local_sticker_files(sticker_dir, include_gif=True)
        items: list[dict[str, Any]] = []
        for file_path in files:
            entry = metadata.get(file_path.name) or {}
            if not isinstance(entry, dict):
                entry = {}
            raw_entry = raw_manifest.get(file_path.name) if isinstance(raw_manifest.get(file_path.name), dict) else {}
            # labeled 判定：原始 manifest 中 description 非空才算"已标"
            # （load_sticker_metadata 会用文件名兜底填 description，不能直接用规范化后的结果判断）
            raw_description = str((raw_entry or {}).get("description", "") or "").strip()
            items.append({
                "filename": file_path.name,
                "size_bytes": file_path.stat().st_size if file_path.exists() else 0,
                "thumbnail_url": f"/personification/api/stickers/file/{file_path.name}",
                "description": raw_description or str(entry.get("description", "") or ""),
                "mood_tags": list(entry.get("mood_tags") or []),
                "scene_tags": list(entry.get("scene_tags") or []),
                "proactive_send": bool(entry.get("proactive_send", False)),
                "use_hint": str(entry.get("use_hint", "") or ""),
                "avoid_hint": str(entry.get("avoid_hint", "") or ""),
                "weight": float(entry.get("weight", 1.0) or 1.0),
                "style": str(entry.get("style", "") or ""),
                "labeled_at": str(entry.get("labeled_at", "") or ""),
                "labeled": bool(raw_description),
            })
        items.sort(key=lambda x: x["filename"].lower())
        return {
            "stickers": items,
            "total": len(items),
            "labeled_count": sum(1 for it in items if it["labeled"]),
            "sticker_dir": str(sticker_dir),
        }

    @router.get("/file/{name}")
    async def get_sticker_file(
        name: str,
        _: AdminIdentity = Depends(require_admin),
    ):
        sticker_dir = _sticker_dir(runtime)
        path = _resolve_safe_file(name, sticker_dir)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if path.suffix.lower() not in _ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=400, detail="不支持的文件类型")
        return FileResponse(path)

    @router.patch("/{name}")
    async def update_sticker(
        name: str,
        body: dict = Body(default_factory=dict),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        sticker_dir = _sticker_dir(runtime)
        path = _resolve_safe_file(name, sticker_dir)
        if not path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        metadata = load_sticker_metadata(sticker_dir)
        existing = metadata.get(name) if isinstance(metadata.get(name), dict) else {}
        # 合并新字段
        merged = dict(existing)
        editable_keys = ("description", "mood_tags", "scene_tags", "proactive_send",
                         "use_hint", "avoid_hint", "weight", "style")
        for key in editable_keys:
            if key in body:
                merged[key] = body[key]
        # 走 normalize 做白名单/范围校验
        merged = normalize_sticker_entry(merged, file_name=name)
        metadata[name] = merged
        save_sticker_metadata_sync(sticker_dir, metadata)
        return {"success": True, "filename": name, "entry": merged}

    @router.delete("/{name}")
    async def delete_sticker(
        name: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        sticker_dir = _sticker_dir(runtime)
        path = _resolve_safe_file(name, sticker_dir)
        if not path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        trash_dir = sticker_dir / "trash" / time.strftime("%Y%m%d")
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest = trash_dir / name
        try:
            shutil.move(str(path), str(dest))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"移动到 trash 失败：{exc}")
        # 从 manifest 移除
        metadata = load_sticker_metadata(sticker_dir)
        metadata.pop(name, None)
        save_sticker_metadata_sync(sticker_dir, metadata)
        webui_audit_log.record(
            action="sticker_delete",
            qq=admin.qq,
            device_id=admin.device_id,
            target=name,
            detail={"trash_path": str(dest)},
        )
        return {"success": True, "trash_path": str(dest)}

    if _multipart_available():
        @router.post("/upload")
        async def upload_sticker(
            file: UploadFile = File(...),
            description: str = Form(default=""),
            admin: AdminIdentity = Depends(require_admin),
        ) -> dict:
            filename = (file.filename or "").strip() or f"sticker_{int(time.time())}.png"
            suffix = Path(filename).suffix.lower()
            if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
                raise HTTPException(status_code=400, detail=f"不支持的扩展名：{suffix}")
            payload = await file.read()
            if not payload:
                raise HTTPException(status_code=400, detail="空文件")
            if len(payload) > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"文件过大（>{_MAX_UPLOAD_BYTES // 1024 // 1024}MB）")
            sticker_dir = _sticker_dir(runtime)
            # 防覆盖：若同名存在加时间戳后缀
            target = sticker_dir / filename
            if target.exists():
                target = sticker_dir / f"{Path(filename).stem}_{int(time.time())}{suffix}"
            try:
                target.write_bytes(payload)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"写入失败：{exc}")
            # manifest 加占位条目（description 可由用户填或后续 labeler 补）
            metadata = load_sticker_metadata(sticker_dir)
            metadata[target.name] = normalize_sticker_entry(
                {"description": description},
                file_name=target.name,
                file_hash=compute_file_hash(payload),
            )
            save_sticker_metadata_sync(sticker_dir, metadata)
            webui_audit_log.record(
                action="sticker_upload",
                qq=admin.qq,
                device_id=admin.device_id,
                target=target.name,
                detail={"size_bytes": len(payload), "has_description": bool(description)},
            )
            return {
                "success": True,
                "filename": target.name,
                "size_bytes": len(payload),
                "needs_labeling": not description,
            }
    else:
        # 优雅降级：缺 python-multipart 时只注册占位端点，
        # 用户调用 /upload 会得到 503 + 安装提示；其他端点（列表 / 编辑 / 删除 / 重扫）保持可用。
        @router.post("/upload")
        async def upload_sticker_unavailable(
            admin: AdminIdentity = Depends(require_admin),
        ) -> dict:
            raise HTTPException(
                status_code=503,
                detail=(
                    "上传功能未启用：bot 进程缺少 python-multipart 依赖。"
                    "在 bot 的 venv 中执行：pip install python-multipart 后重启。"
                ),
            )

    @router.post("/rescan")
    async def rescan_stickers(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """触发 labeler 重新扫描表情包目录。
        mode=missing_only（默认）仅扫描无 description 的文件；force_all 全部重打标。
        实际打标逻辑在 sticker_labeler skill 启动钩子中；此处只是清空指定文件的 manifest 条目，
        让下次启动或后台 labeler 扫描时重新分析。
        """
        mode = str((body or {}).get("mode", "missing_only") or "missing_only")
        if mode not in {"missing_only", "force_all"}:
            raise HTTPException(status_code=400, detail="mode 只能是 missing_only 或 force_all")
        sticker_dir = _sticker_dir(runtime)
        # 直接读写 raw json，避免 normalize 重新填回兜底 description
        raw_manifest = _load_raw_manifest(sticker_dir)
        files = list_local_sticker_files(sticker_dir, include_gif=True)
        cleared = 0
        meta_block = raw_manifest.get("_meta") if isinstance(raw_manifest.get("_meta"), dict) else None
        new_manifest: dict[str, Any] = {}
        for name, entry in raw_manifest.items():
            if name == "_meta":
                continue
            entry_dict = entry if isinstance(entry, dict) else {}
            desc = str(entry_dict.get("description", "") or "").strip()
            if mode == "force_all":
                new_manifest[name] = {}
                cleared += 1
            elif not desc:
                new_manifest[name] = {}
                cleared += 1
            else:
                new_manifest[name] = entry_dict
        # 文件存在但 manifest 没记录的也算未打标
        existing_names = {f.name for f in files}
        for name in existing_names - set(new_manifest.keys()):
            new_manifest[name] = {}
            cleared += 1
        if meta_block:
            new_manifest["_meta"] = meta_block
        path = sticker_metadata_path(sticker_dir)
        path.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        webui_audit_log.record(
            action="sticker_rescan",
            qq=admin.qq,
            device_id=admin.device_id,
            detail={"mode": mode, "scheduled": cleared},
        )
        return {
            "success": True,
            "mode": mode,
            "scheduled": cleared,
            "hint": "已清空对应条目；下次插件启动或后台 labeler 扫描时会自动重新打标。",
        }

    return router
