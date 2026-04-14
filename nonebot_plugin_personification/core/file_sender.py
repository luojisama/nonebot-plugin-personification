from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class QQFileSender:
    """NapCat/OneBot v11 文件发送封装，供后续 skill 复用。"""

    def __init__(self, *, get_bots: Callable[[], dict[str, Any]], logger: Any) -> None:
        self._get_bots = get_bots
        self._logger = logger

    def _normalize_name(self, file: str, name: str | None = None) -> str:
        candidate = str(name or "").strip()
        if candidate:
            return candidate
        path_name = Path(str(file or "").strip()).name
        return path_name or "file.bin"

    async def _iter_bots(self) -> list[Any]:
        try:
            bots = self._get_bots() or {}
        except Exception:
            bots = {}
        return list(bots.values())

    async def upload_group_file(
        self,
        *,
        group_id: str | int,
        file: str,
        name: str | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        normalized_file = str(file or "").strip()
        if not normalized_file:
            raise ValueError("missing file")

        payload = {
            "group_id": int(group_id) if str(group_id).isdigit() else str(group_id),
            "file": normalized_file,
            "name": self._normalize_name(normalized_file, name),
        }
        folder_name = str(folder or "").strip()
        if folder_name:
            payload["folder"] = folder_name

        last_error: Exception | None = None
        for bot in await self._iter_bots():
            try:
                result = await bot.call_api("upload_group_file", **payload)
                return result if isinstance(result, dict) else {"ok": True, "result": result}
            except Exception as e:
                last_error = e
                self._logger.debug(f"[file_sender] upload_group_file failed on bot {getattr(bot, 'self_id', '?')}: {e}")
        if last_error is not None:
            raise last_error
        raise RuntimeError("no available bot for upload_group_file")

    async def upload_private_file(
        self,
        *,
        user_id: str | int,
        file: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        normalized_file = str(file or "").strip()
        if not normalized_file:
            raise ValueError("missing file")

        payload = {
            "user_id": int(user_id) if str(user_id).isdigit() else str(user_id),
            "file": normalized_file,
            "name": self._normalize_name(normalized_file, name),
        }

        last_error: Exception | None = None
        for bot in await self._iter_bots():
            try:
                result = await bot.call_api("upload_private_file", **payload)
                return result if isinstance(result, dict) else {"ok": True, "result": result}
            except Exception as e:
                last_error = e
                self._logger.debug(f"[file_sender] upload_private_file failed on bot {getattr(bot, 'self_id', '?')}: {e}")
        if last_error is not None:
            raise last_error
        raise RuntimeError("no available bot for upload_private_file")


def build_file_sender(*, get_bots: Callable[[], dict[str, Any]], logger: Any) -> QQFileSender:
    return QQFileSender(get_bots=get_bots, logger=logger)


__all__ = ["QQFileSender", "build_file_sender"]
