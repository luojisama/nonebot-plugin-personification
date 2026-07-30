from __future__ import annotations

from datetime import datetime
from pathlib import Path

from plugin.personification.core.web_grounding import do_web_search
from plugin.personification.skill_runtime.runtime_api import SkillRuntime
from . import impl
from ...vision_analyze.scripts import impl as vision_impl


SKILL_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = SKILL_ROOT.parent


class _SkillLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def _compact_visual_search_context(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        import json

        payload = json.loads(text)
    except Exception:
        return text[:300]
    if not isinstance(payload, dict):
        return text[:300]
    parts: list[str] = []
    summary = str(payload.get("scene_summary", "") or "").strip()
    if summary:
        parts.append(summary)
    for item in list(payload.get("ocr_text") or [])[:4]:
        value = str(item or "").strip()
        if value:
            parts.append(f"文字:{value}")
    for item in list(payload.get("characters_or_entities") or [])[:4]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        evidence = str(item.get("evidence", "") or "").strip()
        if name:
            parts.append(f"{name} {evidence}".strip())
    return "；".join(parts)[:300]


async def run(query: str) -> str:
    q = str(query or "").strip()
    if not q:
        return "请提供搜索关键词。"
    try:
        config = impl.load_web_search_config(SKILLS_ROOT)
        prefix = str(config.get("search_prompt_prefix", "") or "").strip()
        final_query = f"{prefix} {q}".strip() if prefix else q
        return await do_web_search(final_query, get_now=lambda: datetime.now(), logger=_SkillLogger())
    except Exception as e:
        return f"联网搜索失败: {e}"


def build_tools(runtime: SkillRuntime):
    skills_root_raw = getattr(runtime.plugin_config, "personification_skills_path", None)
    skills_root = Path(skills_root_raw) if skills_root_raw else None

    async def _visual_query_builder(query: str, image_refs: list[str]) -> str:
        try:
            visual_raw = await vision_impl.analyze_images(
                runtime=runtime,
                query="为联网搜索提取图片中的主体、文字、人物、作品名和关键视觉线索。",
                images=image_refs,
            )
        except Exception as exc:
            runtime.logger.debug(f"[web_search] visual query augmentation skipped: {exc}")
            return query
        visual_context = _compact_visual_search_context(visual_raw)
        if not visual_context:
            return query
        return f"{query} 图像线索：{visual_context}".strip()

    return [
        impl.build_web_search_tool(
            skills_root=skills_root,
            get_now=runtime.get_now,
            logger=runtime.logger,
            plugin_config=runtime.plugin_config,
            visual_query_builder=_visual_query_builder,
        )
    ]
