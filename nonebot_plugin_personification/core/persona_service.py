from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_sync


# 画像判断取最近 20 条本人发言作为上下文，避免被早期无关内容稀释
_PERSONA_CONTEXT_LIMIT = 20


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
【称呼与昵称】：希望被怎么称呼、自称习惯、给别人起的外号；没有就写"信息不足"。
【关系与亲密度】：与 bot 的熟悉/信任程度，是否把 bot 当朋友，互动是客气还是随意；含演变趋势。
【雷区与禁忌】：会让对方明显不适、反感或情绪激动的话题/措辞/玩笑，需要回避的点。
【记忆锚点】：值得长期记住的具体事实——宠物、工作/学校变动、重要的人、纪念日、近期目标等（仅记用户主动透露的）。
【近期关注】：最近这段时间反复出现、明显在意或投入的事（追的作品、在做的项目、烦心事等）。
【内容偏好】：喜欢什么样的回应——幽默还是认真、长还是短、爱不爱表情包、希望被夸还是被吐槽。
【互动建议】：和这个用户聊天的最佳方式——给一个虚拟伙伴看，告诉它该用什么语气、避开什么话题、什么时候适合主动。
【人物描述】：用 150-220 字综合总结，把上面字段串成自然语段，描述性格、习惯、辨识度高的特征。

边界：只基于聊天记录里用户主动透露或明显流露的内容刻画；不臆测、不编造敏感信息
（真实住址、真实姓名、证件号、政治/宗教立场、健康隐私等），这类除非用户明确说过，
否则一律写"信息不足"。这是为了更懂用户、更好地陪伴，不做任何越界推断。"""

_PERSONA_TASK_FRAME = """\
这是一个角色扮演陪伴机器人「更懂用户、更好地陪伴」的内部理解任务：根据群聊/私聊公开
发言，归纳对方的说话风格与偏好。这不是真实身份识别、不涉及隐私侵犯，属于正常的产品
功能。请务必直接完成任务并按格式输出，不要拒绝、不要输出"无法完成/不在能力范围/请
咨询其他问题"之类的话；信息不足的字段写"信息不足"即可。

判断要点（重要）：
- 只刻画【该用户本人】。群里在讨论某话题（如考试、游戏、工作）不代表用户就属于该群体——
  讨论考试的可能是学生，也可能是老师、家长或回忆往事的上班族。
- 优先依据用户的【自述、视角与用词】判断身份（如"我当年""我带的学生""我同事""我孩子"
  "下班""上课"），而不是话题本身或别人的发言。
- 证据冲突或不足时，保守判断或写"信息不足"，不要被单一热门话题带偏。"""

PERSONA_PROMPT_NEW = """\
你是一个用户画像分析师。{task_frame}

请基于下方【该用户本人】的聊天记录刻画其特征。
要求：实话实说，不必赞美；证据不足的字段写"信息不足"，不要为了完整而编造。

{field_guide}

该用户本人的聊天记录（共 {message_count} 条，越靠后越近期）：
{messages_block}"""

PERSONA_PROMPT_UPDATE = """\
你是一个用户画像分析师。{task_frame}

该用户已有一份画像（见「旧画像」），现在请基于最新聊天记录"修订"它。

修订规则：
1. 旧画像中**未被新记录推翻**的事实、判断、特征**必须保留**——不要因为新记录没提就抹除。
2. 仅在新记录里出现**明确证据**时才更新某个字段；语气从"原 X，现 Y"的形式呈现演变。
3. 不要为了显得有变化而编造新内容；信息不足时复用旧字段原文。
4. 每个字段都要给出最终版本（即"保留 + 修订"后的整体），不要只写差异。
5. 如旧画像缺失某字段（比如旧版只有 4 个字段），按新格式补全；缺乏证据的字段写"信息不足"。
6. 旧画像里带「用户确认/用户更正」标记的内容是用户本人核对过的事实，优先级最高，
   除非新记录明确推翻，否则必须原样保留。

{field_guide}

旧画像：
{previous_persona}

该用户本人的最新聊天记录（共 {message_count} 条，越靠后越近期）：
{messages_block}"""


# 画像文本【字段】→ 结构化 key（用于持久化与查询；性别/职业等半永久字段）
_STRUCTURED_FIELD_MAP: dict[str, str] = {
    "职业推测": "occupation", "年龄推测": "age_group", "性别推测": "gender",
    "作息特征": "routine", "兴趣领域": "interests", "沟通风格": "communication_style",
    "情绪基线": "emotion_baseline", "社交模式": "social_mode", "知识结构": "knowledge",
    "称呼与昵称": "nickname_pref", "关系与亲密度": "relationship", "雷区与禁忌": "taboos",
    "记忆锚点": "memory_anchors", "近期关注": "recent_focus", "内容偏好": "content_pref",
    "人物描述": "portrait", "互动建议": "interaction_advice",
}

_STRUCTURED_LINE = re.compile(r"[【\[]\s*([^】\]]+?)\s*[】\]]\s*[:：]?\s*(.+)")


def parse_persona_structured(text: str) -> dict[str, str]:
    """把画像文本里的【字段】：内容 解析成结构化字典（确定性解析，不调用 LLM）。"""
    out: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        m = _STRUCTURED_LINE.match(raw_line.strip())
        if not m:
            continue
        label = m.group(1).strip()
        value = m.group(2).strip()
        key = _STRUCTURED_FIELD_MAP.get(label)
        if key and value and value not in {"信息不足", "未知", "不明"}:
            out[key] = value[:200]
    return out


def _format_persona_prompt(template: str, **kwargs: str) -> str:
    return template.format(field_guide=_PERSONA_FIELD_GUIDE, task_frame=_PERSONA_TASK_FRAME, **kwargs)


def build_persona_prompt(messages: list[str], previous: str | None) -> str:
    # 取最近 N 条本人发言作为判断依据（越靠后越近期）
    recent = list(messages)[-_PERSONA_CONTEXT_LIMIT:]
    messages_block = "\n".join(f"- {message}" for message in recent)
    message_count = str(len(recent))
    if previous:
        return _format_persona_prompt(
            PERSONA_PROMPT_UPDATE,
            previous_persona=str(previous or ""),
            messages_block=messages_block,
            message_count=message_count,
        )
    return _format_persona_prompt(
        PERSONA_PROMPT_NEW, messages_block=messages_block, message_count=message_count
    )


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

    async def apply_user_correction(self, user_id: str, corrections: dict[str, str]) -> PersonaEntry | None:
        """用户/管理员对画像的更正：以最高优先级写入，并保留到后续再生成。

        corrections: {中文字段名或key: 修正后的值}。会在画像文本顶部加「用户更正」块
        （带标记），并持久化到 core profile 的 user_corrections，后续 UPDATE 提示词
        会优先保留这些内容。
        """
        uid = str(user_id)
        clean = {str(k).strip(): str(v).strip() for k, v in (corrections or {}).items() if str(v).strip()}
        if not clean:
            return self.get_persona(uid)
        previous = self.get_persona(uid)
        base_text = previous.data if previous else ""
        # 去掉旧的用户更正块，避免重复堆叠
        base_text = re.sub(r"【用户更正[^】]*】[\s\S]*?(?=\n\n|\Z)", "", base_text).strip()
        block_lines = "\n".join(f"- {k}：{v}（用户本人确认）" for k, v in clean.items())
        new_text = f"【用户更正（最高优先级，请始终保留）】\n{block_lines}\n\n{base_text}".strip()
        entry = PersonaEntry(data=new_text, time=int(time.time()))
        await asyncio.to_thread(self._save_persona_sync, uid, entry, False, corrections=clean)
        self._logger.info(f"[user_persona] 用户 {uid} 画像已按用户更正修订：{list(clean.keys())}")
        return entry

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

    def _save_persona_sync(
        self, user_id: str, entry: PersonaEntry, clear_history: bool, *, corrections: dict | None = None
    ) -> None:
        if self._profile_service is not None:
            structured = parse_persona_structured(entry.data)
            existing = None
            try:
                existing = self._profile_service.get_core_profile(str(user_id))
            except Exception:
                existing = None
            # 保留历史用户更正（除非本次显式覆盖）
            prior_corrections: dict = {}
            prior_profile_meta: dict = {}
            if existing is not None and isinstance(getattr(existing, "profile_json", None), dict):
                prior_corrections = dict(existing.profile_json.get("user_corrections", {}) or {})
                prior_profile_meta = dict(existing.profile_json.get("qq_profile", {}) or {})
            if corrections:
                prior_corrections.update(corrections)
            profile_json = {
                "updated_by": "persona_service",
                "structured": structured,
                "user_corrections": prior_corrections,
            }
            if prior_profile_meta:
                profile_json["qq_profile"] = prior_profile_meta
            self._profile_service.upsert_core_profile(
                user_id=str(user_id),
                profile_text=entry.data,
                profile_json=profile_json,
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
            except SafetyRefusalError as e:
                if getattr(e, "source", "") == "api_block":
                    self._logger.warning(
                        f"[user_persona] 用户 {user_id} 画像生成被供应商安全策略拦截"
                        f"（{getattr(e, 'reason', '') or '未知原因'}）：本轮跳过，保留旧画像。"
                        "可考虑切换 provider 或调低敏感内容触发。"
                    )
                else:
                    self._logger.info(
                        f"[user_persona] 用户 {user_id} 画像 LLM 返回拒绝模板，本轮跳过。"
                    )
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
