from __future__ import annotations

import hashlib
import json
from typing import Any


def build_plugin_source_bundle(source_snapshot: dict[str, Any]) -> str:
    chunks = source_snapshot.get("chunks", [])
    if not isinstance(chunks, list) or not chunks:
        return ""

    ordered_chunks = [
        chunk
        for chunk in chunks
        if isinstance(chunk, dict) and str(chunk.get("text", "") or "").strip()
    ]
    ordered_chunks.sort(
        key=lambda item: (
            str(item.get("file", "") or ""),
            int(item.get("start_line", 0) or 0),
        )
    )

    parts: list[str] = []
    for chunk in ordered_chunks:
        rel_path = str(chunk.get("file", "") or "unknown.py")
        start_line = int(chunk.get("start_line", 1) or 1)
        end_line = int(chunk.get("end_line", start_line) or start_line)
        text = str(chunk.get("text", "") or "").rstrip()
        if not text:
            continue
        segment = f"# === {rel_path}:{start_line}-{end_line} ===\n{text}\n"
        parts.append(segment.rstrip())
    return "\n\n".join(parts).strip()


def compute_source_hash(source: Any) -> str:
    if isinstance(source, dict):
        chunks = source.get("chunks", [])
        payload = []
        if isinstance(chunks, list):
            ordered_chunks = [
                chunk for chunk in chunks
                if isinstance(chunk, dict)
            ]
            ordered_chunks.sort(
                key=lambda item: (
                    str(item.get("file", "") or ""),
                    int(item.get("start_line", 0) or 0),
                    str(item.get("chunk_id", "") or ""),
                )
            )
            for chunk in ordered_chunks:
                payload.append(
                    {
                        "chunk_id": str(chunk.get("chunk_id", "") or ""),
                        "file": str(chunk.get("file", "") or ""),
                        "start_line": int(chunk.get("start_line", 0) or 0),
                        "end_line": int(chunk.get("end_line", 0) or 0),
                        "text": str(chunk.get("text", "") or ""),
                    }
                )
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.md5(raw.encode("utf-8")).hexdigest()
    return hashlib.md5(str(source or "").encode("utf-8")).hexdigest()


__all__ = ["build_plugin_source_bundle", "compute_source_hash"]
