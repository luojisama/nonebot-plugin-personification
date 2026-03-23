from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tool_caller import ToolCaller


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


def _build_persona_prompt(messages: list[str], previous: str | None) -> str:
    messages_block = "\n".join(f"- {message}" for message in messages)
    if previous:
        return PERSONA_PROMPT_UPDATE.format(
            previous_persona=previous,
            messages_block=messages_block,
        )
    return PERSONA_PROMPT_NEW.format(messages_block=messages_block)


@dataclass
class PersonaEntry:
    data: str
    time: int

    def to_dict(self) -> dict[str, Any]:
        return {"data": self.data, "time": self.time}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaEntry":
        text = str(data.get("data", "") or "")
        try:
            timestamp = int(data.get("time", 0) or 0)
        except (TypeError, ValueError):
            timestamp = 0
        return cls(data=text, time=timestamp)

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
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_file_override = Path(data_file) if data_file is not None else None
        self._tool_caller = tool_caller
        self._history_max = max(1, int(history_max))
        self._logger = logger
        self._histories: dict[str, list[str]] = {}
        self._personas: dict[str, PersonaEntry] = {}
        self._write_lock = asyncio.Lock()
        self._generating: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def _data_file(self) -> Path:
        if self._data_file_override is not None:
            return self._data_file_override
        return self._data_dir / "user_personas.json"

    @property
    def history_max(self) -> int:
        return self._history_max

    async def load(self) -> None:
        await self._migrate_legacy()
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        if not self._data_file.exists():
            return
        try:
            raw = json.loads(self._data_file.read_text(encoding="utf-8"))
            personas = raw.get("personas", {})
            histories = raw.get("histories", {})
            if isinstance(personas, dict):
                self._personas = {
                    str(user_id): PersonaEntry.from_dict(entry)
                    for user_id, entry in personas.items()
                    if isinstance(entry, dict)
                }
            if isinstance(histories, dict):
                self._histories = {
                    str(user_id): [str(message) for message in messages if str(message).strip()]
                    for user_id, messages in histories.items()
                    if isinstance(messages, list)
                }
        except Exception as e:
            self._logger.warning(f"[user_persona] 加载数据失败: {e}")

    async def _migrate_legacy(self) -> None:
        new_path = self._data_file
        legacy_path = Path("data/user_persona/data.json")

        def _migrate() -> bool:
            if new_path.exists() or not legacy_path.exists():
                return False
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True

        try:
            migrated = await asyncio.to_thread(_migrate)
        except Exception as e:
            self._logger.warning(f"[user_persona] 旧数据迁移失败: {e}")
            return
        if migrated:
            self._logger.info(f"[user_persona] 已从旧路径迁移数据到 {new_path}，原文件保留。")

    def get_persona(self, user_id: str) -> PersonaEntry | None:
        return self._personas.get(str(user_id))

    def get_persona_text(self, user_id: str) -> str:
        try:
            entry = self.get_persona(str(user_id))
            return entry.data if entry else ""
        except Exception:
            return ""

    def get_persona_snippet(self, user_id: str, max_chars: int = 150) -> str:
        try:
            entry = self.get_persona(str(user_id))
            return entry.snippet(max_chars) if entry else ""
        except Exception:
            return ""

    def get_history_count(self, user_id: str) -> int:
        return len(self._histories.get(str(user_id), []))

    async def record_message(self, user_id: str, text: str) -> None:
        content = str(text or "").strip()
        if not content:
            return

        uid = str(user_id)
        history = self._histories.setdefault(uid, [])
        history.append(content)

        if len(history) >= self._history_max:
            if uid in self._generating:
                await self._flush_histories()
                return

            history_snapshot = history.copy()
            self._histories[uid] = []
            await self._flush_histories()
            self._generating.add(uid)

            task = asyncio.create_task(self._generate_and_save(uid, history_snapshot))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        if len(history) % 10 == 0:
            await self._flush_histories()

    async def force_refresh(self, user_id: str) -> PersonaEntry | None:
        uid = str(user_id)
        if uid in self._generating:
            self._logger.warning(f"[user_persona] 用户 {uid} 画像正在生成，跳过重复刷新")
            return self._personas.get(uid)

        history = list(self._histories.get(uid, []))
        if not history:
            return None

        self._generating.add(uid)
        try:
            previous = self._personas.get(uid)
            result = await self._call_persona_llm(history, previous)
            if not result:
                return None

            entry = PersonaEntry(data=result, time=int(time.time()))
            self._personas[uid] = entry
            self._histories[uid] = []
            await self._persist()
            return entry
        finally:
            self._generating.discard(uid)

    async def _generate_and_save(self, user_id: str, history: list[str]) -> None:
        try:
            previous = self._personas.get(user_id)
            result = await self._call_persona_llm(history, previous)
            if result:
                self._personas[user_id] = PersonaEntry(
                    data=result,
                    time=int(time.time()),
                )
                await self._persist()
                self._logger.info(f"[user_persona] 用户 {user_id} 画像生成成功")
                return

            existing = self._histories.get(user_id, [])
            self._histories[user_id] = history + existing
            await self._flush_histories()
            self._logger.warning(f"[user_persona] 用户 {user_id} 画像生成失败")
        except Exception as e:
            existing = self._histories.get(user_id, [])
            self._histories[user_id] = history + existing
            await self._flush_histories()
            self._logger.warning(f"[user_persona] 生成异常: {e}")
        finally:
            self._generating.discard(user_id)

    async def _call_persona_llm(
        self,
        messages: list[str],
        previous: PersonaEntry | None,
    ) -> str | None:
        prompt = _build_persona_prompt(messages, previous.data if previous else None)
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

    async def _flush_histories(self) -> None:
        await self._persist()

    async def _persist(self) -> None:
        async with self._write_lock:
            payload = {
                "personas": {
                    user_id: entry.to_dict()
                    for user_id, entry in self._personas.items()
                },
                "histories": {
                    user_id: messages
                    for user_id, messages in self._histories.items()
                    if messages
                },
            }

            def _write() -> None:
                self._data_file.parent.mkdir(parents=True, exist_ok=True)
                self._data_file.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            await asyncio.to_thread(_write)
