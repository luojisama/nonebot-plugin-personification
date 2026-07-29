from __future__ import annotations

import os
import re
import time
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any


_PROCESS_STARTED_AT = int(time.time())
_WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BUILD_COMMIT_ENV_NAME = "PERSONIFICATION_BUILD_COMMIT"


def _normalized_commit(value: Any) -> str:
    commit = str(value or "").strip()
    return commit.lower() if _COMMIT_RE.fullmatch(commit) else ""


def _git_dir(repo_root: Path) -> Path | None:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        content = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not content.lower().startswith(prefix):
        return None
    target = Path(content[len(prefix):].strip())
    return target if target.is_absolute() else (repo_root / target).resolve()


def _read_git_commit(repo_root: Path) -> str:
    git_dir = _git_dir(repo_root)
    if git_dir is None:
        return ""
    storage_dirs = [git_dir]
    try:
        common_value = (git_dir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        common_value = ""
    if common_value:
        common_dir = Path(common_value)
        common_dir = common_dir if common_dir.is_absolute() else (git_dir / common_dir).resolve()
        if common_dir not in storage_dirs:
            storage_dirs.append(common_dir)
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    direct = _normalized_commit(head)
    if direct:
        return direct
    if not head.startswith("ref:"):
        return ""
    ref_name = head[4:].strip().replace("\\", "/")
    if not ref_name or ref_name.startswith("/") or ".." in ref_name.split("/"):
        return ""
    for storage_dir in storage_dirs:
        try:
            commit = _normalized_commit(
                (storage_dir / Path(ref_name)).read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            commit = ""
        if commit:
            return commit
    for storage_dir in storage_dirs:
        try:
            packed_refs = (storage_dir / "packed-refs").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            continue
        for line in packed_refs:
            if not line or line.startswith(("#", "^")):
                continue
            commit, _, packed_ref = line.partition(" ")
            if packed_ref.strip() == ref_name:
                return _normalized_commit(commit)
    return ""


def _resolve_build_commit() -> str:
    commit = _normalized_commit(os.environ.get(_BUILD_COMMIT_ENV_NAME))
    if commit:
        return commit
    return _read_git_commit(Path(__file__).resolve().parents[1])


def _resolve_version() -> str:
    for package_name in (
        "nonebot-plugin-shiro-personification",
        "nonebot-plugin-personification",
    ):
        try:
            value = str(metadata.version(package_name) or "").strip()
        except metadata.PackageNotFoundError:
            continue
        if value:
            return value[:80]
    return ""


_BUILD_COMMIT = _resolve_build_commit()
_PACKAGE_VERSION = _resolve_version()


def get_runtime_identity() -> dict[str, Any]:
    build_id = _BUILD_COMMIT[:12] or (_PACKAGE_VERSION and f"version-{_PACKAGE_VERSION}") or "source-unknown"
    return {
        "build_id": build_id,
        "git_commit": _BUILD_COMMIT,
        "package_version": _PACKAGE_VERSION,
        "worker_id": _WORKER_ID,
        "process_started_at": _PROCESS_STARTED_AT,
    }


__all__ = ["get_runtime_identity"]
