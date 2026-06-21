"""一次性迁移：为存量 memory_items 按当前状态分配 tier 字段。

用法（在 personification 仓库根执行）：

    # dry-run，仅打印将要发生的变更，不写库
    python -m personification.scripts.migrate_memory_tiers --dry-run

    # 实际迁移；自动备份 memory_palace.db 到 memory_palace.db.bak.<ts>
    python -m personification.scripts.migrate_memory_tiers --apply

只在 plugin_config.personification_data_dir 下查找 memory_palace.db。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def _resolve_palace_path() -> Path | None:
    """通过 personification.core.paths 解析 memory_palace.db 路径。"""
    try:
        from personification.core.paths import get_data_dir
    except Exception:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from personification.core.paths import get_data_dir  # type: ignore
        except Exception as exc:
            print(f"[migrate] 无法 import personification.core.paths: {exc}", file=sys.stderr)
            return None
    try:
        data_dir = Path(get_data_dir(None))
    except Exception as exc:
        print(f"[migrate] get_data_dir 失败: {exc}", file=sys.stderr)
        return None
    candidate = data_dir / "memory_palace" / "memory_palace.db"
    if not candidate.exists():
        print(f"[migrate] memory_palace.db 不存在: {candidate}", file=sys.stderr)
        return None
    return candidate


def _classify(payload: dict, now_ts: float) -> str:
    try:
        from personification.core.memory_tier import classify_initial_tier
    except Exception:
        # 回退：内联简版
        mtype = str(payload.get("memory_type", "") or "").lower()
        if mtype in {
            "session_summary", "daily_summary", "group_knowledge", "group_meme",
            "concept_anchor", "user_persona", "persona_knowledge", "core_profile",
            "fact", "semantic", "group_relation",
        }:
            return "semantic"
        if int(payload.get("reinforcement_count", 0) or 0) >= 3:
            return "semantic"
        if int(payload.get("access_count", 0) or 0) >= 5:
            return "semantic"
        created = float(payload.get("time_created", 0) or 0)
        age = now_ts - created if created > 0 else 0
        return "working" if age <= 24 * 3600 else "episodic"
    return classify_initial_tier(payload, now_ts=now_ts)


def migrate(db_path: Path, *, dry_run: bool) -> dict:
    now_ts = time.time()
    if not dry_run:
        bak = db_path.with_suffix(f".db.bak.{int(now_ts)}")
        shutil.copy2(db_path, bak)
        print(f"[migrate] 备份完成: {bak}")
    counts: dict[str, int] = {"working": 0, "episodic": 0, "semantic": 0, "background": 0}
    untouched = 0
    written = 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT memory_id, payload FROM memory_items").fetchall()
        for row in rows:
            mid = str(row["memory_id"] or "")
            if not mid:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            current_tier = str(payload.get("tier", "") or "").strip().lower()
            if current_tier in counts:
                # 已有 tier，跳过（手动迁移完后重跑不会重复操作）
                counts[current_tier] += 1
                untouched += 1
                continue
            new_tier = _classify(payload, now_ts)
            counts[new_tier] = counts.get(new_tier, 0) + 1
            if dry_run:
                continue
            payload["tier"] = new_tier
            conn.execute(
                "UPDATE memory_items SET payload=? WHERE memory_id=?",
                (json.dumps(payload, ensure_ascii=False), mid),
            )
            written += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return {"counts": counts, "written": written, "untouched_already_tagged": untouched, "total": sum(counts.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description="为存量 memory_items 分配 tier")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="只统计不写库")
    group.add_argument("--apply", action="store_true", help="实际写入（自动备份）")
    args = parser.parse_args()

    db_path = _resolve_palace_path()
    if db_path is None:
        return 1
    print(f"[migrate] memory_palace.db = {db_path}")
    result = migrate(db_path, dry_run=args.dry_run)
    print("[migrate] 结果:")
    print(f"  总条数: {result['total']}")
    print(f"  已有 tier（跳过）: {result['untouched_already_tagged']}")
    print(f"  写入条数: {result['written']}")
    for tier, n in sorted(result["counts"].items()):
        print(f"  {tier}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
