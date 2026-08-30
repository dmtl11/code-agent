from __future__ import annotations

import ast
import re
from pathlib import Path


IGNORED_PARTS = {".git", "__pycache__", ".venv", ".code_agent_build"}
SOURCE_SUFFIXES = {".py", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".js", ".ts"}


class RepoMap:
    """Build a compact, deterministic map of files and important symbols."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    def build(self, query: str = "", max_chars: int = 6000) -> str:
        if not self.workspace.exists():
            return "Repository map: workspace is empty."

        rows: list[tuple[int, str]] = []
        terms = {term.lower() for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)}
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workspace)
            if any(part in IGNORED_PARTS for part in rel.parts):
                continue
            entry = self._entry(path, rel)
            haystack = entry.lower()
            score = sum(3 for term in terms if term in rel.as_posix().lower())
            score += sum(1 for term in terms if term in haystack)
            rows.append((score, entry))

        if not rows:
            return "Repository map: workspace is empty."

        rows.sort(key=lambda item: (-item[0], item[1].lower()))
        header = "Repository map (compact file and symbol index):"
        output = header
        omitted = 0
        for _, entry in rows:
            candidate = f"{output}\n{entry}"
            if len(candidate) > max(400, max_chars):
                omitted += 1
                continue
            output = candidate
        if omitted:
            output += f"\n... {omitted} lower-priority files omitted by map budget"
        return output

    def _entry(self, path: Path, rel: Path) -> str:
        size = path.stat().st_size
        first = f"{rel.as_posix()} ({size} B)"
        if path.suffix.lower() not in SOURCE_SUFFIXES or size > 300_000:
            return first
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return first

        symbols = self._symbols(path.suffix.lower(), text)
        if not symbols:
            return first
        return "\n".join([first, *[f"  {symbol}" for symbol in symbols[:24]]])

    def _symbols(self, suffix: str, text: str) -> list[str]:
        if suffix == ".py":
            return self._python_symbols(text)
        if suffix in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
            return self._pattern_symbols(
                text,
                [
                    (r"^\s*(?:class|struct)\s+([A-Za-z_]\w*)", "type"),
                    (
                        r"^\s*(?:[\w:<>,~*&]+\s+)+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?\{?\s*$",
                        "function",
                    ),
                ],
            )
        return self._pattern_symbols(
            text,
            [
                (r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
                (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
                (r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=.*=>", "function"),
            ],
        )

    def _python_symbols(self, text: str) -> list[str]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return ["syntax: invalid Python (AST unavailable)"]

        symbols: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(f"function {node.name} (line {node.lineno})")
            elif isinstance(node, ast.ClassDef):
                symbols.append(f"class {node.name} (line {node.lineno})")
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(f"method {node.name}.{child.name} (line {child.lineno})")
        return symbols

    def _pattern_symbols(self, text: str, patterns: list[tuple[str, str]]) -> list[str]:
        symbols: list[str] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern, kind in patterns:
                match = re.match(pattern, line)
                if match:
                    symbols.append(f"{kind} {match.group(1)} (line {line_number})")
                    break
        return symbols
