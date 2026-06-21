#!/usr/bin/env python
"""拟人插件回放集执行工具。

用法：
    python -m plugin.personification.scripts.replay_corpus \\
        --input plugin/personification/tests/replay_corpus/*.jsonl \\
        --output replay_report.md

读取 jsonl 回放语料，对每段：
- 加载 messages + metadata
- 调用 metadata_fallback_turn_plan 生成新 plan
- 与 expected_frame 对比，输出差异
- 不调用真实 LLM（避免成本和波动）

输出 markdown 报表，列出每段 diff 与命中情况，作为 PR 审查参考。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ReplayRecord:
    file: str
    line_no: int
    scene: str
    expected_frame: dict[str, Any]
    metadata: dict[str, Any]
    expected_reply: str = ""
    note: str = ""


@dataclass
class ReplayDiff:
    record: ReplayRecord
    actual_plan: dict[str, Any]
    diffs: list[str] = field(default_factory=list)


def load_records(paths: list[str]) -> list[ReplayRecord]:
    records: list[ReplayRecord] = []
    expanded: list[str] = []
    for pattern in paths:
        expanded.extend(sorted(glob.glob(pattern, recursive=True)))
    if not expanded:
        return records
    for file_path in expanded:
        path = Path(file_path)
        if not path.exists() or path.suffix != ".jsonl":
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"[warn] {file_path}:{line_no} JSON 解析失败: {exc}", file=sys.stderr)
                    continue
                if not isinstance(data, dict):
                    continue
                metadata = data.get("metadata", {}) or {}
                records.append(
                    ReplayRecord(
                        file=str(file_path),
                        line_no=line_no,
                        scene=str(data.get("scene", "") or ""),
                        expected_frame=dict(data.get("expected_frame", {}) or {}),
                        metadata=dict(metadata),
                        expected_reply=str(data.get("expected_reply", "") or ""),
                        note=str(metadata.get("note", "") or ""),
                    )
                )
    return records


_PLANNER_MODULE = None


def _load_planner() -> Any:
    """直接按文件加载 planner.py，避免触发 personification __init__.py 的 nonebot 依赖。"""
    global _PLANNER_MODULE
    if _PLANNER_MODULE is not None:
        return _PLANNER_MODULE
    import importlib.util

    planner_path = Path(__file__).resolve().parent.parent / "agent" / "runtime" / "planner.py"
    spec = importlib.util.spec_from_file_location("personification_planner", planner_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 planner 模块: {planner_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["personification_planner"] = module
    spec.loader.exec_module(module)
    _PLANNER_MODULE = module
    return module


def compute_actual_plan(record: ReplayRecord) -> dict[str, Any]:
    """走元数据 fallback 生成 plan，不调用 LLM。"""
    planner = _load_planner()
    metadata_fallback_turn_plan = planner.metadata_fallback_turn_plan

    md = record.metadata
    plan = metadata_fallback_turn_plan(
        is_group=not bool(md.get("is_private", False)),
        is_random_chat=bool(md.get("is_random_chat", False)),
        is_direct_mention=bool(md.get("is_direct_mention", False)),
        has_images=bool(md.get("has_images", False)),
        message_target=str(md.get("message_target", "") or ""),
        qzone_event_type=str(md.get("qzone_event_type", "") or ""),
    )
    return {
        "reply_action": plan.reply_action,
        "memory_need": plan.memory_need,
        "research_need": plan.research_need,
        "vision_need": plan.vision_need,
        "qzone_continue": plan.qzone_continue,
        "output_mode": plan.output_mode,
        "tool_intent": list(plan.tool_intent),
        "ambiguity_level": plan.ambiguity_level,
        "message_target": plan.message_target,
        "session_goal": plan.session_goal,
        "reason": plan.reason,
    }


_PLAN_TO_FRAME_KEY_MAP = {
    "ambiguity_level": "ambiguity_level",
    "output_mode": "output_mode",
}


def diff_plan_vs_expected(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    diffs: list[str] = []
    for plan_key, frame_key in _PLAN_TO_FRAME_KEY_MAP.items():
        if frame_key not in expected:
            continue
        expected_val = expected.get(frame_key)
        actual_val = actual.get(plan_key)
        if expected_val != actual_val:
            diffs.append(f"{frame_key}: expected={expected_val!r}, plan={actual_val!r}")
    # 对 recommend_silence 与 reply_action 的对比
    if "recommend_silence" in expected:
        expected_silence = bool(expected.get("recommend_silence"))
        plan_silence = actual.get("reply_action") == "silence"
        if expected_silence != plan_silence:
            diffs.append(f"silence: expected={expected_silence}, plan={plan_silence} (action={actual.get('reply_action')!r})")
    return diffs


def render_report(diffs: list[ReplayDiff]) -> str:
    total = len(diffs)
    aligned = sum(1 for d in diffs if not d.diffs)
    misaligned = total - aligned
    lines: list[str] = [
        "# 拟人插件回放对比报表",
        "",
        f"- 样本总数：{total}",
        f"- 元数据 fallback 与历史 frame 一致：{aligned}",
        f"- 有 diff 的样本：{misaligned}",
        "",
        "**说明**：本报表仅对比 metadata fallback 输出与历史 frame，未调用 LLM。"
        "差异是预期的（fallback 必须保守），仅作为 PR 审查参考，不做硬断言。",
        "",
        "## 按场景分布",
        "",
    ]
    scene_counts: dict[str, int] = {}
    for d in diffs:
        scene_counts[d.record.scene] = scene_counts.get(d.record.scene, 0) + 1
    for scene, count in sorted(scene_counts.items()):
        lines.append(f"- {scene or 'unknown'}: {count}")
    lines.append("")
    lines.append("## 差异明细")
    lines.append("")
    for d in diffs:
        if not d.diffs:
            continue
        lines.append(f"### {d.record.file}:{d.record.line_no} [{d.record.scene}]")
        if d.record.note:
            lines.append(f"> 备注：{d.record.note}")
        lines.append(f"- 期望 frame: `{json.dumps(d.record.expected_frame, ensure_ascii=False)}`")
        lines.append(f"- 元数据 plan: `{json.dumps(d.actual_plan, ensure_ascii=False)}`")
        lines.append(f"- 期望回复: `{d.record.expected_reply}`")
        for diff_line in d.diffs:
            lines.append(f"  - {diff_line}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="拟人插件回放对比工具")
    parser.add_argument(
        "--input",
        nargs="+",
        default=["plugin/personification/tests/replay_corpus/*.jsonl"],
        help="jsonl 文件 glob 模式，可多个",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="输出报表路径；'-' 表示打印到 stdout",
    )
    args = parser.parse_args()

    records = load_records(args.input)
    if not records:
        print("[warn] 未匹配到任何回放样本", file=sys.stderr)
        return 1

    diffs: list[ReplayDiff] = []
    for record in records:
        actual_plan = compute_actual_plan(record)
        diff = ReplayDiff(record=record, actual_plan=actual_plan)
        diff.diffs = diff_plan_vs_expected(actual_plan, record.expected_frame)
        diffs.append(diff)

    report = render_report(diffs)
    if args.output == "-" or args.output == "":
        print(report)
    else:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
