from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_sync


_PERSONA_FIELD_GUIDE = """\
要求输出格式严格如下（不要使用 Markdown 符号 / # *，每段一行或多行）：
【职业推测】：身份/职业/学业阶段，证据不足直接写"信息不足"，不要瞎猜。
【年龄推测】：粗略年龄段（如"20 代前半""中学生""社会人"），含判断依据。
【性别推测】：男 / 女 / 不明；含判断依据。
【作息特征】：活跃时段、是否昼夜颠倒、聊天密度（高频灌水 / 偶尔出没）。
【兴趣领域】：明确观察到的兴趣（游戏 / ACG / 编程 / 学术 / 时事 / 美食 / 健身 / 学习 / 写作 / 运动等），具体到名词。
【沟通风格】：语气特点、常用口头禅或感叹词、是否爱玩梗、表情包使用偏好、句子长度。
【情绪基线】：常态情绪 / 抗压表现 / 容易被点燃或低落的话题。
【社交模式】：主动还是被动、对陌生人态度、群聊还是私聊更多、是否爱 @ 别人。
【知识结构】：能看出来的专业/技能领域（"对 Python 有较深理解""熟悉东方系列""二次元历史/作品如数家珍"等）。
【互动建议】：和这个用户聊天的最佳方式——给一个虚拟伙伴看，告诉它该用什么语气、避开什么话题、什么时候适合主动。
【人物描述】：用 120-180 字综合总结，把上面字段串成自然语段，描述性格、习惯、辨识度高的特征。"""

PERSONA_PROMPT_NEW = """\
你是一个用户画像分析师。请基于下方聊天记录刻画该用户特征。
要求：实话实说，不必赞美；证据不足的字段写"信息不足"，不要为了完整而编造。

{field_guide}

用户聊天记录：
{messages_block}"""

PERSONA_PROMPT_UPDATE = """\
你是一个用户画像分析师。该用户已有一份画像（见「旧画像」），现在请基于最新聊天记录"修订"它。

修订规则：
1. 旧画像中**未被新记录推翻**的事实、判断、特征**必须保留**——不要因为新记录没提就抹除。
2. 仅在新记录里出现**明确证据**时才更新某个字段；语气从"原 X，现 Y"的形式呈现演变。
3. 不要为了显得有变化而编造新内容；信息不足时复用旧字段原文。
4. 每个字段都要给出最终版本（即"保留 + 修订"后的整体），不要只写差异。
5. 如旧画像缺失某字段（比如旧版只有 4 个字段），按新格式补全；缺乏证据的字段写"信息不足"。

{field_guide}

旧画像：
{previous_persona}

最新聊天记录：
{messages_block}"""


def _format_persona_prompt(template: str, **kwargs: str) -> str:
    return template.format(field_guide=_PERSONA_FIELD_GUIDE, **kwargs)


def build_persona_prompt(messages: list[str], previous: str | None) -> str:
    messages_block = "\n".join(f"- {message}" for message in messages)
    if previous:
        return _format_persona_prompt(
            PERSONA_PROMPT_UPDATE,
            previous_persona=str(previous or ""),
            messages_block=messages_block,
        )
    return _format_persona_prompt(PERSONA_PROMPT_NEW, messages_block=messages_block)


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
            result = await self._call_persona_llm(history, previous, user_id=str(user_id))
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
            result = await self._call_persona_llm(history, previous, user_id=str(user_id))
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

    async def _call_persona_llm(
        self,
        messages: list[str],
        previous: PersonaEntry | None,
        *,
        user_id: str = "",
    ) -> str | None:
        prompt = build_persona_prompt(messages, previous.data if previous else None)
        retry_prompt = (
            prompt
            + "\n\n（提醒：请只基于上述聊天记录客观提炼字段，不要给出'抱歉'、"
            "'作为AI'、'无法回答'等套话；信息不足的字段直接写'信息不足'。）"
        )
        token = None
        try:
            from .llm_context import reset_llm_context, set_llm_context

            token = set_llm_context(purpose="user_persona", user_id=str(user_id or ""))
        except Exception:
            token = None
        try:
            from .safety_filter import SafetyRefusalError, sanitize_or_retry
            from .token_ledger import record_response_usage

            async def _first() -> Any:
                return await self._tool_caller.chat_with_tools(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    use_builtin_search=False,
                )

            async def _retry() -> Any:
                return await self._tool_caller.chat_with_tools(
                    messages=[{"role": "user", "content": retry_prompt}],
                    tools=[],
                    use_builtin_search=False,
                )

            try:
                response = await sanitize_or_retry(
                    call=_first,
                    retry_call=_retry,
                    on_response=record_response_usage,
                    logger=self._logger,
                    purpose="user_persona",
                )
            except SafetyRefusalError:
                return None
        except Exception as e:
            self._logger.warning(f"[user_persona] LLM 调用失败: {e}")
            return None
        finally:
            if token is not None:
                try:
                    reset_llm_context(token)
                except Exception:
                    pass
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
