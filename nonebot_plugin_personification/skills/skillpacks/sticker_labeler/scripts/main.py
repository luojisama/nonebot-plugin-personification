from __future__ import annotations

from pathlib import Path

from . import impl as legacy


async def run(
    action: str,
    file_path: str = "",
    raw_json: str = "",
    model_name: str = "",
) -> str:
    act = str(action or "").strip().lower()
    if act == "to_data_url":
        path = Path(file_path)
        if not path.exists():
            return "文件不存在"
        return legacy.image_file_to_data_url(path)
    if act == "normalize":
        if not raw_json.strip():
            return "请提供 raw_json"
        try:
            normalized = legacy.normalize_label_result(raw_json, model_name=model_name)
            import json

            return json.dumps(normalized, ensure_ascii=False)
        except Exception as e:
            return f"normalize 失败: {e}"
    return "action 可选: to_data_url, normalize"

