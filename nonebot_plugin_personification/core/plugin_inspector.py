from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from nonebot.plugin import Plugin
except Exception:  # pragma: no cover
    Plugin = Any  # type: ignore[misc,assignment]

from ..skills.skillpacks.tool_caller.scripts.impl import ToolCaller


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


def _safe_read_text(path: Path, max_chars: int = _MAX_FILE_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    except Exception:
        return ""
    return text[:max_chars]


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


def _iter_plugin_python_files(plugin_root: Path) -> list[Path]:
    if plugin_root.is_file():
        return [plugin_root]

    files: list[Path] = []
    init_path = plugin_root / "__init__.py"
    if init_path.exists():
        files.append(init_path)

    for path in sorted(plugin_root.rglob("*.py")):
        if path == init_path:
            continue
        if "migrations" in path.parts:
            continue
        if path.name in _SKIP_FILE_NAMES:
            continue
        files.append(path)
    return files


def extract_plugin_skeleton(plugin_root: Path) -> str:
    files = _iter_plugin_python_files(plugin_root)
    if not files:
        return ""

    prioritized: list[tuple[Path, str]] = []
    for path in files:
        body = _extract_module_skeleton(path)
        if not body:
            continue
        label = path.name if plugin_root.is_file() else path.relative_to(plugin_root).as_posix()
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


async def analyze_plugin_with_llm(
    skeleton: str,
    plugin_name: str,
    tool_caller: ToolCaller,
) -> dict:
    prompt = (
        "你是 NoneBot2 插件分析器。"
        "根据给出的插件代码骨架，输出严格 JSON。"
        "不要输出 markdown，不要输出代码块，不要输出解释文字。\n\n"
        "输出结构必须为：\n"
        "{\n"
        '  "display_name": "插件中文名或常用名",\n'
        '  "summary": "一句话描述（50字以内）",\n'
        '  "keywords": ["用户可能提到此插件时说的词，10个以内"],\n'
        '  "triggers": [\n'
        '    {"type": "command|keyword|message|notice", "pattern": "触发方式", "description": "..."}\n'
        "  ],\n"
        '  "features": {\n'
        '    "feature_key": {\n'
        '      "title": "功能中文名",\n'
        '      "keywords": ["触发词"],\n'
        '      "summary": "功能简介（80字以内）",\n'
        '      "config_items": ["配置项名"],\n'
        '      "detail": "详细说明含配置示例（300字以内）"\n'
        "    }\n"
        "  },\n"
        '  "config_schema": {\n'
        '    "CONFIG_KEY": {"type": "str|int|bool|float", "default": "...", "description": "..."}\n'
        "  },\n"
        '  "dependencies": ["依赖的其他插件名"]\n'
        "}\n\n"
        f"插件名: {plugin_name}\n"
        "请基于以下代码骨架分析：\n"
        f"{skeleton}"
    )

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await tool_caller.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                use_builtin_search=False,
            )
            data = json.loads(str(response.content or "").strip())
            if not isinstance(data, dict):
                raise ValueError("llm result is not a JSON object")
            return data
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                prompt += "\n\n再次提醒：只输出一个 JSON 对象，不要包含任何额外文本。"
    raise ValueError(f"analyze_plugin_with_llm failed: {last_error}")


def scan_runtime_data(plugin_name: str, data_base_dir: Path) -> dict | None:
    root = data_base_dir / plugin_name
    if not root.exists() or not root.is_dir():
        return None

    files: list[dict[str, Any]] = []
    notable_files: dict[str, dict[str, Any]] = {}

    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if len(rel_parts) > 3:
                continue
            rel_path = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            entry = {"path": rel_path, "size": size}
            files.append(entry)
            if path.suffix.lower() != ".json" or size >= 50 * 1024:
                continue
            try:
                raw_text = path.read_text(encoding="utf-8")
                parsed = json.loads(raw_text)
                notable_files[rel_path] = {
                    "size": size,
                    "preview": json.dumps(parsed, ensure_ascii=False)[:500],
                }
            except Exception:
                continue
    except Exception:
        return None

    return {
        "data_dir": f"data/{plugin_name}",
        "files": files,
        "notable_files": notable_files,
    }
