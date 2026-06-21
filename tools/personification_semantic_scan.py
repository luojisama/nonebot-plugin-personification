from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence


DEFAULT_TARGETS = (
    "nonebot_plugin_personification/agent/runtime/runner.py",
    "nonebot_plugin_personification/core/chat_intent.py",
    "nonebot_plugin_personification/core/evolves.py",
    "nonebot_plugin_personification/core/target_inference.py",
    "nonebot_plugin_personification/agent/runtime/planner.py",
    "nonebot_plugin_personification/handlers/reply_pipeline/pipeline_emotion.py",
    "nonebot_plugin_personification/handlers/yaml_pipeline/processor.py",
)

_TEXT_NAME_RE = re.compile(
    r"(?:^|_)(text|message|utterance|content|query|reply|candidate|merged|plain|normalized)(?:_|$)",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_RE = re.compile(r"(?:^|_)(?:KEYWORDS|HINTS|PHRASES|TRIGGERS)$")
_NON_SEMANTIC_SOURCE_RE = re.compile(r"(?:DIAGNOSTIC|ERROR|STATUS|CONTROL|MARKER|SCHEMA|ROUTE)", re.IGNORECASE)
_STRUCTURAL_JSON_FUNCTIONS = {
    "extract_json_payload",
    "_parse_json_payload",
    "_parse_review_payload",
    "_parse_image_classifier_payload",
    "_looks_like_translation_result",
    "_group_translation_result",
}


@dataclass(frozen=True)
class SemanticScanViolation:
    path: Path
    line: int
    code: str
    message: str

    def render(self, root: Path) -> str:
        try:
            rel_path = self.path.relative_to(root)
        except ValueError:
            rel_path = self.path
        return f"{rel_path}:{self.line}: {self.code}: {self.message}"


class _SemanticVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[SemanticScanViolation] = []
        self._function_stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for name in _assigned_names(target):
                if _looks_like_keyword_source(name) and _is_string_collection(node.value):
                    self._add(
                        node,
                        "semantic-keyword-table",
                        f"{name} looks like a dialogue semantic keyword table; move this judgement to LLM metadata.",
                    )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        for name in _assigned_names(node.target):
            if _looks_like_keyword_source(name) and node.value is not None and _is_string_collection(node.value):
                self._add(
                    node,
                    "semantic-keyword-table",
                    f"{name} looks like a dialogue semantic keyword table; move this judgement to LLM metadata.",
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _call_name(node.func) == "any":
            for arg in node.args:
                if isinstance(arg, ast.GeneratorExp) and self._generator_uses_keyword_source_on_text(arg):
                    self._add(
                        node,
                        "semantic-keyword-any",
                        "any(keyword in text for keyword in KEYWORDS) is not allowed in dialogue semantics.",
                    )
        if self._call_is_regex_keyword_search(node):
            self._add(
                node,
                "semantic-regex-search",
                "regex keyword search over dialogue text is not allowed in semantic modules.",
            )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._inside_structural_json_parser():
            self.generic_visit(node)
            return
        left = node.left
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                if isinstance(left, ast.Constant) and isinstance(left.value, str) and _references_dialogue_text(comparator):
                    if _literal_membership_is_structural(left.value, comparator):
                        left = comparator
                        continue
                    self._add(
                        node,
                        "semantic-literal-membership",
                        "literal membership check against dialogue text is not allowed.",
                    )
                if _is_string_collection(left) and _references_dialogue_text(comparator):
                    self._add(
                        node,
                        "semantic-literal-membership",
                        "literal collection membership check against dialogue text is not allowed.",
                    )
            left = comparator
        self.generic_visit(node)

    def _generator_uses_keyword_source_on_text(self, node: ast.GeneratorExp) -> bool:
        if self._inside_structural_json_parser():
            return False
        iter_uses_keyword_source = any(_looks_like_keyword_source(_expr_name(gen.iter)) for gen in node.generators)
        if not iter_uses_keyword_source:
            return False
        return _contains_keyword_membership_on_text(node.elt) or any(
            _contains_keyword_membership_on_text(if_expr)
            for gen in node.generators
            for if_expr in gen.ifs
        )

    def _call_is_regex_keyword_search(self, node: ast.Call) -> bool:
        if self._inside_structural_json_parser():
            return False
        name = _call_name(node.func)
        if name not in {"re.search", "re.match", "re.fullmatch"}:
            return False
        if len(node.args) < 2:
            return False
        pattern = node.args[0]
        target = node.args[1]
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, str):
            return False
        if not _references_dialogue_text(target):
            return False
        pattern_text = pattern.value
        structural_markers = ("\\{", "\\[", "```", "</", "<", "^\\s*$")
        if any(marker in pattern_text for marker in structural_markers):
            return False
        return bool(re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", pattern_text))

    def _inside_structural_json_parser(self) -> bool:
        return bool(self._function_stack and self._function_stack[-1] in _STRUCTURAL_JSON_FUNCTIONS)

    def _add(self, node: ast.AST, code: str, message: str) -> None:
        self.violations.append(
            SemanticScanViolation(
                path=self.path,
                line=int(getattr(node, "lineno", 1) or 1),
                code=code,
                message=message,
            )
        )


def scan_paths(paths: Iterable[Path | str]) -> list[SemanticScanViolation]:
    violations: list[SemanticScanViolation] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            file_paths = sorted(path.rglob("*.py"))
        else:
            file_paths = [path]
        for file_path in file_paths:
            if not file_path.exists() or "__pycache__" in file_path.parts:
                continue
            try:
                tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            except SyntaxError as exc:
                violations.append(
                    SemanticScanViolation(
                        path=file_path,
                        line=int(exc.lineno or 1),
                        code="syntax-error",
                        message=str(exc),
                    )
                )
                continue
            visitor = _SemanticVisitor(file_path)
            visitor.visit(tree)
            violations.extend(visitor.violations)
    return sorted(violations, key=lambda item: (str(item.path), item.line, item.code))


def default_target_paths(root: Path) -> list[Path]:
    return [root / item for item in DEFAULT_TARGETS]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan personification semantic modules for keyword decisions.")
    parser.add_argument("paths", nargs="*", help="Files or directories to scan. Defaults to core semantic modules.")
    args = parser.parse_args(list(argv or []))
    root = Path.cwd()
    paths = [Path(item) for item in args.paths] if args.paths else default_target_paths(root)
    violations = scan_paths(paths)
    if violations:
        for violation in violations:
            print(violation.render(root))
        return 1
    print("personification semantic scan passed")
    return 0


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in node.elts:
            names.extend(_assigned_names(item))
        return names
    return []


def _looks_like_keyword_source(name: str) -> bool:
    return bool(name and _KEYWORD_SOURCE_RE.search(name) and not _NON_SEMANTIC_SOURCE_RE.search(name))


def _is_string_collection(node: ast.AST) -> bool:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts)
    return False


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _call_name(node: ast.AST) -> str:
    return _expr_name(node)


def _literal_membership_is_structural(literal: str, target: ast.AST) -> bool:
    if literal.startswith(("[", "<")):
        return True
    if literal == "戳一戳" and _expr_name(target) == "action_text":
        return True
    return False


def _references_dialogue_text(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and _TEXT_NAME_RE.search(child.id):
            return True
        if isinstance(child, ast.Attribute) and _TEXT_NAME_RE.search(child.attr):
            return True
    return False


def _contains_keyword_membership_on_text(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Compare):
            continue
        left = child.left
        for op, comparator in zip(child.ops, child.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and _references_dialogue_text(comparator):
                return True
            if isinstance(op, (ast.In, ast.NotIn)) and _references_dialogue_text(left):
                return True
            left = comparator
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
