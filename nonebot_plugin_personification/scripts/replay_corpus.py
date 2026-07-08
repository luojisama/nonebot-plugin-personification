#!/usr/bin/env python
"""拟人插件回放集执行工具。

用法：
    python plugin/personification/scripts/replay_corpus.py \\
        --input plugin/personification/tests/replay_corpus/*.jsonl \\
        --output replay_report.md

读取 jsonl 回放语料，对每段：
- 加载 messages + metadata
- 调用 metadata_fallback_turn_plan 生成新 plan
- 与 expected_frame 对比，输出差异
- 汇总人工标注的坏回复样例、失败标签和回复边界
- 不调用真实 LLM（避免成本和波动）

输出 markdown 报表，列出每段 diff 与命中情况，作为 PR 审查参考。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import types
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
    bad_reply_examples: list[dict[str, Any]] = field(default_factory=list)
    quality_tags: list[str] = field(default_factory=list)
    reply_boundary: str = ""
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
                bad_reply_examples = data.get("bad_reply_examples", []) or []
                quality_tags = data.get("quality_tags", []) or []
                records.append(
                    ReplayRecord(
                        file=str(file_path),
                        line_no=line_no,
                        scene=str(data.get("scene", "") or ""),
                        expected_frame=dict(data.get("expected_frame", {}) or {}),
                        metadata=dict(metadata),
                        expected_reply=str(data.get("expected_reply", "") or ""),
                        bad_reply_examples=list(bad_reply_examples) if isinstance(bad_reply_examples, list) else [],
                        quality_tags=[str(item or "").strip() for item in quality_tags if str(item or "").strip()]
                        if isinstance(quality_tags, list)
                        else [],
                        reply_boundary=str(data.get("reply_boundary", "") or ""),
                        note=str(metadata.get("note", "") or ""),
                    )
                )
    return records


_PLANNER_MODULE = None


def _ensure_namespace_package(name: str, path: Path) -> None:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = module
        return
    if not hasattr(module, "__path__"):
        module.__path__ = [str(path)]  # type: ignore[attr-defined]


def _load_planner() -> Any:
    """按 namespace 包加载 planner.py，避免触发 personification __init__.py。"""
    global _PLANNER_MODULE
    if _PLANNER_MODULE is not None:
        return _PLANNER_MODULE
    import importlib.util

    personification_dir = Path(__file__).resolve().parent.parent
    plugin_dir = personification_dir.parent
    _ensure_namespace_package("plugin", plugin_dir)
    _ensure_namespace_package("plugin.personification", personification_dir)
    _ensure_namespace_package("plugin.personification.agent", personification_dir / "agent")
    _ensure_namespace_package("plugin.personification.agent.runtime", personification_dir / "agent" / "runtime")
    _ensure_namespace_package("plugin.personification.core", personification_dir / "core")

    module_name = "plugin.personification.agent.runtime.planner"
    planner_path = personification_dir / "agent" / "runtime" / "planner.py"
    spec = importlib.util.spec_from_file_location(module_name, planner_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 planner 模块: {planner_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
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
        "speech_act": plan.speech_act,
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
    "speech_act": "speech_act",
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


def build_report_payload(diffs: list[ReplayDiff]) -> dict[str, Any]:
    scene_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    boundary_counts: dict[str, int] = {}
    bad_examples_by_tag: dict[str, int] = {}
    diff_count_by_tag: dict[str, int] = {}
    diff_count_by_boundary: dict[str, int] = {}

    for diff in diffs:
        record = diff.record
        scene_key = record.scene or "unknown"
        scene_counts[scene_key] = scene_counts.get(scene_key, 0) + 1
        if record.reply_boundary:
            boundary_counts[record.reply_boundary] = boundary_counts.get(record.reply_boundary, 0) + 1
            if diff.diffs:
                diff_count_by_boundary[record.reply_boundary] = diff_count_by_boundary.get(record.reply_boundary, 0) + 1
        for tag in record.quality_tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            if diff.diffs:
                diff_count_by_tag[tag] = diff_count_by_tag.get(tag, 0) + 1
        for example in record.bad_reply_examples:
            if not isinstance(example, dict):
                continue
            label = str(example.get("label", "") or "").strip()
            if not label:
                continue
            bad_examples_by_tag[label] = bad_examples_by_tag.get(label, 0) + 1

    quality_rows = []
    for tag in sorted(set(tag_counts) | set(bad_examples_by_tag)):
        quality_rows.append(
            {
                "tag": tag,
                "samples": tag_counts.get(tag, 0),
                "bad_examples": bad_examples_by_tag.get(tag, 0),
                "diff_samples": diff_count_by_tag.get(tag, 0),
            }
        )

    boundary_rows = []
    for boundary in sorted(boundary_counts):
        boundary_rows.append(
            {
                "boundary": boundary,
                "samples": boundary_counts.get(boundary, 0),
                "diff_samples": diff_count_by_boundary.get(boundary, 0),
            }
        )

    detail_rows = []
    for diff in diffs:
        detail_rows.append(
            {
                "file": diff.record.file,
                "line_no": diff.record.line_no,
                "scene": diff.record.scene,
                "note": diff.record.note,
                "expected_frame": diff.record.expected_frame,
                "actual_plan": diff.actual_plan,
                "expected_reply": diff.record.expected_reply,
                "reply_boundary": diff.record.reply_boundary,
                "quality_tags": list(diff.record.quality_tags),
                "bad_reply_examples": list(diff.record.bad_reply_examples),
                "diffs": list(diff.diffs),
            }
        )

    total = len(diffs)
    aligned = sum(1 for item in diffs if not item.diffs)
    return {
        "summary": {
            "total": total,
            "aligned": aligned,
            "misaligned": total - aligned,
            "bad_reply_examples": sum(len(item.record.bad_reply_examples) for item in diffs),
        },
        "scene_counts": dict(sorted(scene_counts.items())),
        "quality_coverage": quality_rows,
        "reply_boundary_coverage": boundary_rows,
        "details": detail_rows,
    }


def render_json_report(diffs: list[ReplayDiff]) -> str:
    return json.dumps(build_report_payload(diffs), ensure_ascii=False, indent=2)


def render_report(diffs: list[ReplayDiff]) -> str:
    payload = build_report_payload(diffs)
    summary = payload["summary"]
    lines: list[str] = [
        "# 拟人插件回放对比报表",
        "",
        f"- 样本总数：{summary['total']}",
        f"- 元数据 fallback 与历史 frame 一致：{summary['aligned']}",
        f"- 有 diff 的样本：{summary['misaligned']}",
        f"- 标注坏回复样例：{summary['bad_reply_examples']}",
        "",
        "**说明**：本报表仅对比 metadata fallback 输出与历史 frame，未调用 LLM。"
        "差异是预期的（fallback 必须保守），仅作为 PR 审查参考，不做硬断言。",
        "",
        "## 按场景分布",
        "",
    ]
    for scene, count in payload["scene_counts"].items():
        lines.append(f"- {scene}: {count}")
    lines.append("")
    if payload["quality_coverage"]:
        lines.append("## 质量覆盖矩阵")
        lines.append("")
        lines.append("| 标签 | 样本 | 坏例 | fallback diff |")
        lines.append("|---|---:|---:|---:|")
        for row in payload["quality_coverage"]:
            lines.append(
                f"| {row['tag']} | {row['samples']} | {row['bad_examples']} | {row['diff_samples']} |"
            )
        lines.append("")
    if payload["reply_boundary_coverage"]:
        lines.append("## 回复边界")
        lines.append("")
        lines.append("| 边界 | 样本 | fallback diff |")
        lines.append("|---|---:|---:|")
        for row in payload["reply_boundary_coverage"]:
            lines.append(f"| {row['boundary']} | {row['samples']} | {row['diff_samples']} |")
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
        if d.record.reply_boundary:
            lines.append(f"- 回复边界: `{d.record.reply_boundary}`")
        if d.record.quality_tags:
            lines.append(f"- 质量标签: `{', '.join(d.record.quality_tags)}`")
        if d.record.bad_reply_examples:
            examples = [
                {
                    "label": str(item.get("label", "") or ""),
                    "text": str(item.get("text", "") or ""),
                    "why": str(item.get("why", "") or ""),
                }
                for item in d.record.bad_reply_examples
                if isinstance(item, dict)
            ]
            lines.append(f"- 坏回复样例: `{json.dumps(examples, ensure_ascii=False)}`")
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
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="输出格式：markdown 或 json",
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

    report = render_json_report(diffs) if args.format == "json" else render_report(diffs)
    if args.output == "-" or args.output == "":
        print(report)
    else:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
