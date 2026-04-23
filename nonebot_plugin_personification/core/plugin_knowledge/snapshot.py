from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

try:
    from nonebot.plugin import Plugin
except Exception:  # pragma: no cover
    Plugin = Any  # type: ignore[misc,assignment]

from .bundle import build_plugin_source_bundle, compute_source_hash
from .runtime_scan import scan_runtime_data


_SKIP_FILE_NAMES = {"utils.py", "helpers.py", "models.py", "db.py", "database.py"}
_MATCHER_NAMES = {
    "on_command",
    "on_keyword",
    "on_message",
    "on_notice",
    "on_regex",
    "on_startswith",
    "on_endswith",
    "on_fullmatch",
    "on_shell_command",
    "on_type",
    "on_request",
    "on_metaevent",
}
_MAX_FILE_CHARS = 200 * 1024
_MAX_SKELETON_CHARS = 24_000
_MAX_SOURCE_CHUNK_LINES = 120
_MAX_SOURCE_CHUNK_CHARS = 4_000
_SMALL_PLUGIN_MAX_SOURCE_CHARS = 32_000
_SMALL_PLUGIN_MAX_SOURCE_CHUNKS = 24
_SMALL_PLUGIN_MAX_FILES = 12
_MODULE_BUNDLE_MAX_SOURCE_CHARS = 20_000
_MODULE_BUNDLE_MAX_FILES = 8
_HIGH_VALUE_FILE_HINTS = (
    "__init__",
    "config",
    "runtime",
    "matcher",
    "handler",
    "command",
    "store",
    "provider",
    "service",
)
_HIGH_VALUE_SYMBOL_HINTS = (
    "config",
    "register",
    "build_",
    "matcher",
    "handle_",
    "command",
    "store",
    "provider",
)


def _classify_module_key(rel_path: str) -> tuple[str, str]:
    normalized = str(rel_path or "").strip().replace("\\", "/")
    if not normalized:
        return "misc", "misc"
    parts = [part for part in normalized.split("/") if part]
    stem = Path(normalized).stem.lower()
    if normalized == "__init__.py":
        return "bootstrap", "bootstrap"
    if parts and parts[0] == "skills" and len(parts) >= 3 and parts[1] in {"skillpacks", "custom"}:
        key = f"skills/{parts[2]}"
        return key, key
    if parts and parts[0] in {"handlers", "core", "flows", "jobs", "services"}:
        if len(parts) >= 2 and parts[1] != "__init__.py":
            key = f"{parts[0]}/{Path(parts[1]).stem}"
            return key, key
        return parts[0], parts[0]
    if parts and parts[0] in {"adapters", "routers", "matchers"}:
        if len(parts) >= 2 and parts[1] != "__init__.py":
            key = f"{parts[0]}/{Path(parts[1]).stem}"
            return key, key
        return parts[0], parts[0]
    if any(token in stem for token in ("config", "setting")):
        return "config", "config"
    if any(token in stem for token in ("runtime", "bootstrap", "startup")):
        return "runtime", "runtime"
    if any(token in stem for token in ("provider", "service", "client", "api")):
        return "services", "services"
    if any(token in stem for token in ("store", "memory", "db", "database", "repo")):
        return "storage", "storage"
    if any(token in stem for token in ("handler", "command", "matcher", "event")):
        return "interaction", "interaction"
    if len(parts) >= 2:
        key = f"{parts[0]}/{Path(parts[1]).stem}"
        return key, key
    return stem or normalized, stem or normalized


def _build_module_bundles(
    snapshot_files: list[dict[str, Any]],
    snapshot_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    file_to_module: dict[str, tuple[str, str]] = {}
    for item in snapshot_files:
        if not isinstance(item, dict):
            continue
        rel_path = str(item.get("path", "") or "").strip()
        if not rel_path:
            continue
        file_to_module[rel_path] = _classify_module_key(rel_path)

    grouped_chunks: dict[str, list[dict[str, Any]]] = {}
    grouped_labels: dict[str, str] = {}
    for chunk in snapshot_chunks:
        if not isinstance(chunk, dict):
            continue
        rel_path = str(chunk.get("file", "") or "").strip()
        module_key, module_label = file_to_module.get(rel_path, _classify_module_key(rel_path))
        grouped_labels[module_key] = module_label
        grouped_chunks.setdefault(module_key, []).append(chunk)

    module_bundles: list[dict[str, Any]] = []
    for module_key in sorted(grouped_chunks):
        module_label = grouped_labels.get(module_key, module_key)
        ordered_chunks = sorted(
            grouped_chunks[module_key],
            key=lambda item: (
                str(item.get("file", "") or ""),
                int(item.get("start_line", 0) or 0),
                str(item.get("chunk_id", "") or ""),
            ),
        )
        provisional: list[dict[str, Any]] = []
        current_chunk_ids: list[str] = []
        current_files: list[str] = []
        current_chars = 0
        for chunk in ordered_chunks:
            chunk_id = str(chunk.get("chunk_id", "") or "").strip()
            rel_path = str(chunk.get("file", "") or "").strip()
            chunk_text = str(chunk.get("text", "") or "")
            estimated = len(chunk_text) + 80
            next_files = current_files if rel_path in current_files else [*current_files, rel_path]
            if current_chunk_ids and (
                current_chars + estimated > _MODULE_BUNDLE_MAX_SOURCE_CHARS
                or len(next_files) > _MODULE_BUNDLE_MAX_FILES
            ):
                provisional.append(
                    {
                        "module_key": module_key,
                        "module_label": module_label,
                        "chunk_ids": list(current_chunk_ids),
                        "files": list(current_files),
                        "source_chars": current_chars,
                    }
                )
                current_chunk_ids = []
                current_files = []
                current_chars = 0
            if chunk_id:
                current_chunk_ids.append(chunk_id)
            if rel_path and rel_path not in current_files:
                current_files.append(rel_path)
            current_chars += estimated
        if current_chunk_ids:
            provisional.append(
                {
                    "module_key": module_key,
                    "module_label": module_label,
                    "chunk_ids": list(current_chunk_ids),
                    "files": list(current_files),
                    "source_chars": current_chars,
                }
            )
        total_splits = len(provisional)
        for split_index, item in enumerate(provisional, start=1):
            module_bundles.append(
                {
                    "bundle_key": (
                        module_key
                        if total_splits == 1
                        else f"{module_key}#{split_index}"
                    ),
                    "module_key": module_key,
                    "module_label": module_label,
                    "split_index": split_index,
                    "split_total": total_splits,
                    "chunk_ids": list(item.get("chunk_ids") or []),
                    "files": list(item.get("files") or []),
                    "source_chars": int(item.get("source_chars", 0) or 0),
                }
            )
    return module_bundles


def _select_analysis_strategy(
    *,
    snapshot_files: list[dict[str, Any]],
    snapshot_chunks: list[dict[str, Any]],
    total_chars: int,
) -> str:
    if (
        total_chars <= _SMALL_PLUGIN_MAX_SOURCE_CHARS
        and len(snapshot_files) <= _SMALL_PLUGIN_MAX_FILES
        and len(snapshot_chunks) <= _SMALL_PLUGIN_MAX_SOURCE_CHUNKS
    ):
        return "full_source"
    return "module_bundles"


def _safe_read_text(path: Path, max_chars: int | None = _MAX_FILE_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    except Exception:
        return ""
    if isinstance(max_chars, int) and max_chars > 0:
        return text[:max_chars]
    return text


def _is_nonebot_import(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return bool(node.module and node.module.startswith("nonebot"))
    return False


def _is_matcher_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _MATCHER_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in _MATCHER_NAMES
    return False


def _get_docstring_expr(body: list[ast.stmt]) -> ast.Expr | None:
    if not body:
        return None
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first
    return None


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())


def _render_docstring(node: ast.AST, indent: str = "") -> str:
    doc = ast.get_docstring(node, clean=False)
    if not doc:
        return ""
    return f'{indent}"""{doc}"""'


def _render_signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = "..."
        returns = ""
        if getattr(node, "returns", None) is not None:
            try:
                returns = f" -> {ast.unparse(node.returns)}"
            except Exception:
                returns = ""
        return f"{prefix}{node.name}({args}){returns}: ..."
    if isinstance(node, ast.ClassDef):
        bases: list[str] = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                continue
        suffix = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{suffix}:"
    return ""


def _is_constant_assignment(node: ast.AST) -> bool:
    targets: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                targets.append(target.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        targets.append(node.target.id)
    if not targets:
        return False
    return any(name == "__plugin_meta__" or name.isupper() for name in targets)


def _render_assignment(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _render_function(node: ast.AST, indent: str = "") -> str:
    lines = [indent + _render_signature(node)]
    doc = _render_docstring(node, indent + "    ")
    if doc:
        lines.append(doc)
    return "\n".join(line for line in lines if line.strip())


def _render_class(node: ast.ClassDef) -> str:
    lines = [_render_signature(node)]
    doc = _render_docstring(node, "    ")
    if doc:
        lines.append(doc)
    for item in node.body:
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            rendered = _render_assignment(item)
            if rendered:
                lines.append(_indent(rendered))
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_indent(_render_function(item)))
    if len(lines) == 1:
        lines.append("    ...")
    return "\n".join(lines)


def _fallback_meta_only(path: Path) -> str:
    candidates = [path]
    if path.suffix == ".pyc":
        source_candidate = path.with_suffix(".py")
        if source_candidate.exists():
            candidates.insert(0, source_candidate)
        if path.stem == "__init__":
            init_candidate = path.parent / "__init__.py"
            if init_candidate.exists():
                candidates.insert(0, init_candidate)
    for candidate in candidates:
        text = _safe_read_text(candidate)
        if not text:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if "__plugin_meta__" not in line:
                continue
            chunk = [line]
            for extra in lines[idx + 1: idx + 12]:
                if extra and not extra.startswith((" ", "\t", ")", "]", "}")):
                    break
                chunk.append(extra)
            return "\n".join(chunk).strip()
    return ""


def _extract_module_skeleton(path: Path) -> str:
    source = _safe_read_text(path)
    if not source:
        return _fallback_meta_only(path)
    try:
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return _fallback_meta_only(path)

    lines: list[str] = []
    module_doc = _get_docstring_expr(tree.body)
    if module_doc is not None:
        rendered = _render_docstring(tree)
        if rendered:
            lines.append(rendered)

    for node in tree.body:
        if node is module_doc:
            continue
        if _is_nonebot_import(node):
            rendered = _render_assignment(node)
            if rendered:
                lines.append(rendered)
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if _is_constant_assignment(node):
                rendered = _render_assignment(node)
                if rendered:
                    lines.append(rendered)
                continue
            if isinstance(node, ast.Assign) and _is_matcher_call(node.value):
                rendered = _render_assignment(node)
                if rendered:
                    lines.append(rendered)
                continue
        if isinstance(node, ast.Expr) and _is_matcher_call(node.value):
            rendered = _render_assignment(node)
            if rendered:
                lines.append(rendered)
            continue
        if isinstance(node, ast.ClassDef):
            if node.name == "Config":
                lines.append(_render_class(node))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_render_function(node))

    return "\n\n".join(part.strip() for part in lines if str(part).strip()).strip()


def _iter_plugin_python_files(plugin_root: Path, *, include_support_files: bool = False) -> list[Path]:
    if plugin_root.is_file():
        return [plugin_root]

    files: list[Path] = []
    init_path = plugin_root / "__init__.py"
    if init_path.exists():
        files.append(init_path)

    for path in sorted(plugin_root.rglob("*.py")):
        if path == init_path:
            continue
        if "__pycache__" in path.parts:
            continue
        if "migrations" in path.parts:
            continue
        if not include_support_files and path.name in _SKIP_FILE_NAMES:
            continue
        files.append(path)
    return files


def _plugin_file_label(plugin_root: Path, path: Path) -> str:
    return path.name if plugin_root.is_file() else path.relative_to(plugin_root).as_posix()


def _extract_module_symbols(source: str, path: Path) -> list[str]:
    try:
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return []

    symbols: list[str] = []

    def _append(value: str) -> None:
        text = str(value or "").strip()
        if not text or text in symbols:
            return
        symbols.append(text)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _append(node.name)
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _append(target.id)
            if _is_matcher_call(node.value):
                func = node.value.func
                if isinstance(func, ast.Name):
                    _append(func.id)
                elif isinstance(func, ast.Attribute):
                    _append(func.attr)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            _append(node.target.id)
    return symbols


def _chunk_source_text(
    rel_path: str,
    source: str,
    *,
    symbols: list[str],
) -> list[dict[str, Any]]:
    lines = source.splitlines()
    if not lines:
        return []

    chunks: list[dict[str, Any]] = []
    start = 0
    chunk_index = 0
    while start < len(lines):
        end = min(len(lines), start + _MAX_SOURCE_CHUNK_LINES)
        while end > start + 20:
            candidate = "\n".join(lines[start:end])
            if len(candidate) <= _MAX_SOURCE_CHUNK_CHARS:
                break
            end -= 10
        text = "\n".join(lines[start:end]).strip("\n")
        if not text.strip():
            start = end
            continue
        preview = ""
        for line in text.splitlines():
            preview = line.strip()
            if preview:
                break
        chunk_index += 1
        chunks.append(
            {
                "chunk_id": f"{rel_path}#{chunk_index}",
                "file": rel_path,
                "start_line": start + 1,
                "end_line": end,
                "preview": preview[:160],
                "symbols": symbols[:24],
                "text": text,
            }
        )
        start = end
    return chunks


def extract_plugin_skeleton(plugin_root: Path) -> str:
    files = _iter_plugin_python_files(plugin_root)
    if not files:
        return ""

    prioritized: list[tuple[Path, str]] = []
    for path in files:
        body = _extract_module_skeleton(path)
        if not body:
            continue
        label = _plugin_file_label(plugin_root, path)
        segment = body if plugin_root.is_file() and len(files) == 1 else f"# === {label} ===\n{body}"
        prioritized.append((path, segment))

    if not prioritized:
        return ""

    prioritized.sort(key=lambda item: (0 if item[0].name == "__init__.py" else 1, str(item[0])))
    selected: list[str] = []
    total = 0
    for _path, segment in prioritized:
        piece = (segment.strip() + "\n\n")
        if total >= _MAX_SKELETON_CHARS:
            break
        remaining = _MAX_SKELETON_CHARS - total
        if len(piece) <= remaining:
            selected.append(piece.rstrip())
            total += len(piece)
            continue
        clipped = piece[:remaining]
        if clipped.strip():
            selected.append(clipped.rstrip() + "\n# ... truncated ...")
        total = _MAX_SKELETON_CHARS
        break
    return "\n\n".join(part for part in selected if part.strip()).strip()


def get_plugin_root(plugin: Plugin) -> Path | None:
    try:
        module = getattr(plugin, "module", None)
        if module is None:
            return None
        module_name = str(getattr(module, "__name__", "") or "")
        if module_name.startswith("nonebot.plugins"):
            return None
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return None
        path = Path(module_file).resolve()
        if path.name == "__init__.py" or (path.suffix == ".pyc" and path.stem == "__init__"):
            return path.parent
        if path.suffix in {".py", ".pyc"}:
            return path
    except Exception:
        return None
    return None


def compute_skeleton_hash(skeleton: str) -> str:
    return hashlib.md5(skeleton.encode("utf-8")).hexdigest()


def extract_plugin_source_snapshot(plugin_root: Path) -> dict[str, Any] | None:
    files = _iter_plugin_python_files(plugin_root, include_support_files=True)
    if not files:
        return None

    snapshot_files: list[dict[str, Any]] = []
    snapshot_chunks: list[dict[str, Any]] = []
    total_chars = 0

    for path in files:
        source = _safe_read_text(path, max_chars=None)
        if not source.strip():
            continue

        rel_path = _plugin_file_label(plugin_root, path)
        symbols = _extract_module_symbols(source, path)
        chunks = _chunk_source_text(rel_path, source, symbols=symbols)
        if not chunks:
            continue

        preview = ""
        for line in source.splitlines():
            preview = line.strip()
            if preview:
                break

        snapshot_files.append(
            {
                "path": rel_path,
                "size": len(source),
                "line_count": len(source.splitlines()),
                "symbols": symbols[:32],
                "preview": preview[:200],
            }
        )

        for chunk in chunks:
            chunk_text = str(chunk.get("text", "") or "")
            if not chunk_text:
                continue
            snapshot_chunks.append(chunk)
            total_chars += len(chunk_text)

    if not snapshot_files or not snapshot_chunks:
        return None

    module_bundles = _build_module_bundles(snapshot_files, snapshot_chunks)
    analysis_strategy = _select_analysis_strategy(
        snapshot_files=snapshot_files,
        snapshot_chunks=snapshot_chunks,
        total_chars=total_chars,
    )

    return {
        "root_kind": "file" if plugin_root.is_file() else "package",
        "files": snapshot_files,
        "chunks": snapshot_chunks,
        "source_chars": total_chars,
        "source_chunk_count": len(snapshot_chunks),
        "module_bundles": module_bundles,
        "module_bundle_count": len(module_bundles),
        "analysis_strategy": analysis_strategy,
        "complete": True,
    }
