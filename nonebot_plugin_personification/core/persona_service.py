from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..skills.skillpacks.tool_caller.scripts.impl import ToolCaller
from .db import connect_sync


PERSONA_PROMPT_NEW = """\
你是一个专业的人格分析师和用户画像专家。
请根据以下用户最近的聊天记录，分析该用户的特征，实话实说，不要充满谄媚和恭维。

要求输出格式严格如下（不使用任何 Markdown 格式符号，不使用 **、# 等）：
【职业推测】：...
【年龄推测】：...
【性别推测】：...
【人物描述】：（此处要求 150-200 字左右，详细描述性格、语言风格、兴趣爱好等特征）

用户聊天记录如下：
{messages_block}"""

PERSONA_PROMPT_UPDATE = """\
你是一个专业的人格分析师和用户画像专家。
该用户此前已有一份画像（见「旧画像」部分）。
请结合旧画像和以下最新聊天记录，对画像进行更新与完善，实话实说，不要充满谄媚和恭维。
以新记录为主要依据；旧画像中若有新记录未涉及的内容，可酌情保留或合并。

要求输出格式严格如下（不使用任何 Markdown 格式符号，不使用 **、# 等）：
【职业推测】：...
【年龄推测】：...
【性别推测】：...
【人物描述】：（此处要求 150-200 字左右，详细描述性格、语言风格、兴趣爱好等特征）

旧画像：
{previous_persona}

最新聊天记录：
{messages_block}"""


def build_persona_prompt(messages: list[str], previous: str | None) -> str:
    messages_block = "\n".join(f"- {message}" for message in messages)
    if previous:
        return PERSONA_PROMPT_UPDATE.format(previous_persona=previous, messages_block=messages_block)
    return PERSONA_PROMPT_NEW.format(messages_block=messages_block)


@dataclass
class PersonaEntry:
    data: str
    time: int

    def to_dict(self) -> dict[str, Any]:
        return {"data": self.data, "time": self.time}

    def snippet(self, max_chars: int = 150) -> str:
        if max_chars <= 0 or not self.data:
            return ""
        if len(self.data) <= max_chars:
            return self.data
        return f"{self.data[:max_chars]}..."


class PersonaStore:
    def __init__(
        self,
        data_dir: Path,
        tool_caller: ToolCaller,
        history_max: int,
        logger: Any,
        data_file: Path | None = None,
        profile_service: Any = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._tool_caller = tool_caller
        self._history_max = max(1, int(history_max))
        self._logger = logger
        self._profile_service = profile_service
        self._write_lock = asyncio.Lock()
        self._generating: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def history_max(self) -> int:
        return self._history_max

    async def load(self) -> None:
        return None

    def get_persona(self, user_id: str) -> PersonaEntry | None:
        if self._profile_service is not None:
            snapshot = self._profile_service.get_core_profile(str(user_id))
            if snapshot is not None and snapshot.profile_text:
                return PersonaEntry(data=snapshot.profile_text, time=int(snapshot.updated_at))
        with connect_sync() as conn:
            row = conn.execute(
                "SELECT persona, updated_at FROM user_personas WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        if not row:
            return None
        return PersonaEntry(data=str(row["persona"] or ""), time=int(float(row["updated_at"] or 0)))

    def get_persona_text(self, user_id: str) -> str:
        entry = self.get_persona(str(user_id))
        return entry.data if entry else ""

    def get_persona_snippet(self, user_id: str, max_chars: int = 150) -> str:
        entry = self.get_persona(str(user_id))
        return entry.snippet(max_chars) if entry else ""

    def get_history_count(self, user_id: str) -> int:
        with connect_sync() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS cnt FROM persona_histories WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        return int(row["cnt"] if row else 0)

    def _load_history(self, user_id: str) -> list[str]:
        with connect_sync() as conn:
            rows = conn.execute(
                "SELECT content FROM persona_histories WHERE user_id=? ORDER BY created_at ASC, id ASC",
                (str(user_id),),
            ).fetchall()
        return [str(row["content"] or "") for row in rows if str(row["content"] or "").strip()]

    async def record_message(self, user_id: str, text: str) -> None:
        content = str(text or "").strip()
        if not content:
            return
        uid = str(user_id)
        await asyncio.to_thread(self._append_history_sync, uid, content)
        if self.get_history_count(uid) >= self._history_max:
            if uid in self._generating:
                return
            history_snapshot = self._load_history(uid)
            if not history_snapshot:
                return
            self._generating.add(uid)
            task = asyncio.create_task(self._generate_and_save(uid, history_snapshot))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _append_history_sync(self, user_id: str, text: str) -> None:
        with connect_sync() as conn:
            conn.execute(
                "INSERT INTO persona_histories(user_id, content, created_at) VALUES (?, ?, ?)",
                (user_id, text, time.time()),
            )
            conn.commit()

    async def force_refresh(self, user_id: str) -> PersonaEntry | None:
        uid = str(user_id)
        if uid in self._generating:
            self._logger.warning(f"[user_persona] 用户 {uid} 画像正在生成，跳过重复刷新")
            return self.get_persona(uid)
        history = self._load_history(uid)
        if not history:
            return None
        self._generating.add(uid)
        try:
            previous = self.get_persona(uid)
            result = await self._call_persona_llm(history, previous)
            if not result:
                return None
            entry = PersonaEntry(data=result, time=int(time.time()))
            await asyncio.to_thread(self._save_persona_sync, uid, entry, True)
            return entry
        finally:
            self._generating.discard(uid)

    async def _generate_and_save(self, user_id: str, history: list[str]) -> None:
        try:
            previous = self.get_persona(user_id)
            result = await self._call_persona_llm(history, previous)
            if result:
                entry = PersonaEntry(data=result, time=int(time.time()))
                await asyncio.to_thread(self._save_persona_sync, user_id, entry, True)
                self._logger.info(f"[user_persona] 用户 {user_id} 画像生成成功")
                return
            self._logger.warning(f"[user_persona] 用户 {user_id} 画像生成失败")
        except Exception as e:
            self._logger.warning(f"[user_persona] 生成异常: {e}")
        finally:
            self._generating.discard(user_id)

    def _save_persona_sync(self, user_id: str, entry: PersonaEntry, clear_history: bool) -> None:
        if self._profile_service is not None:
            self._profile_service.upsert_core_profile(
                user_id=str(user_id),
                profile_text=entry.data,
                profile_json={"updated_by": "persona_service"},
                source="persona_service",
            )
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO user_personas(user_id, persona, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET persona=excluded.persona, updated_at=excluded.updated_at
                """,
                (user_id, entry.data, float(entry.time)),
            )
            if clear_history:
                conn.execute("DELETE FROM persona_histories WHERE user_id=?", (user_id,))
            conn.commit()

    async def _call_persona_llm(self, messages: list[str], previous: PersonaEntry | None) -> str | None:
        prompt = build_persona_prompt(messages, previous.data if previous else None)
        try:
            response = await self._tool_caller.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                use_builtin_search=False,
            )
        except Exception as e:
            self._logger.warning(f"[user_persona] LLM 调用失败: {e}")
            return None
        text = str(getattr(response, "content", "") or "").strip()
        return text or None

    async def clear_all(self) -> dict[str, int]:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with connect_sync() as conn:
            persona_row = conn.execute("SELECT COUNT(1) AS cnt FROM user_personas").fetchone()
            history_user_row = conn.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM persona_histories").fetchone()
            history_msg_row = conn.execute("SELECT COUNT(1) AS cnt FROM persona_histories").fetchone()
            conn.execute("DELETE FROM user_personas")
            conn.execute("DELETE FROM persona_histories")
            conn.commit()
        self._generating.clear()
        self._tasks.clear()
        return {
            "personas": int(persona_row["cnt"] if persona_row else 0),
            "history_users": int(history_user_row["cnt"] if history_user_row else 0),
            "history_messages": int(history_msg_row["cnt"] if history_msg_row else 0),
            "cancelled_tasks": len(tasks),
        }
