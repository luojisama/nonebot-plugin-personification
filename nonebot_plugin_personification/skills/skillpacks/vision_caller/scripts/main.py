from __future__ import annotations

from types import SimpleNamespace

from . import impl as legacy


async def run(
    action: str = "build",
    api_type: str = "openai",
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    prompt: str = "",
    image_url: str = "",
) -> str:
    cfg = SimpleNamespace(
        personification_labeler_api_type=api_type,
        personification_labeler_api_key=api_key,
        personification_labeler_api_url=base_url,
        personification_labeler_model=model,
        personification_api_type=api_type,
        personification_api_key=api_key,
        personification_api_url=base_url,
        personification_model=model,
    )
    caller = legacy.build_vision_caller(cfg)
    if caller is None:
        return "视觉调用器未构建（缺少配置或不支持的类型）"
    if str(action or "").strip().lower() == "describe":
        if not prompt or not image_url:
            return "describe 需要 prompt 与 image_url"
        try:
            return await caller.describe(prompt, image_url)
        except Exception as e:
            return f"视觉识别失败: {e}"
    return f"已构建视觉调用器: {caller.__class__.__name__}"
