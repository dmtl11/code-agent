from __future__ import annotations

import ast
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


IGNORED_PARTS = {
    ".git",
    ".code_agent",
    ".code_agent_build",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
SOURCE_SUFFIXES = {".py", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".js", ".jsx", ".ts", ".tsx"}
TEXT_SUFFIXES = SOURCE_SUFFIXES | {".html", ".css", ".json", ".md", ".txt", ".toml", ".yaml", ".yml"}
MAX_SYMBOL_BYTES = 1_000_000
REGISTRY_VERSION = "2"


@dataclass(frozen=True)
class RegistryStats:
    files: int
    symbols: int
    relations: int
    updated: int = 0
    removed: int = 0


class CodeRegistry:
    """Persistent, incrementally refreshed file/symbol/relation registry."""

    def __init__(self, workspace: str | Path, db_path: str | Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or self.workspace / ".code_agent" / "code_registry.sqlite3").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    language TEXT NOT NULL,
                    suffix TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS symbols_file_idx ON symbols(file_path, start_line);
                CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name);
                CREATE TABLE IF NOT EXISTS relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    FOREIGN KEY(source_path) REFERENCES files(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS relations_source_idx ON relations(source_path, line);
                CREATE INDEX IF NOT EXISTS relations_target_idx ON relations(target);
                CREATE TABLE IF NOT EXISTS registry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            version = db.execute("SELECT value FROM registry_meta WHERE key = 'version'").fetchone()
            if not version or str(version["value"]) != REGISTRY_VERSION:
                db.execute("UPDATE files SET mtime_ns = -1")
                db.execute(
                    "INSERT INTO registry_meta(key, value) VALUES ('version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (REGISTRY_VERSION,),
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def sync(self) -> RegistryStats:
        disk_paths: set[str] = set()
        updated = 0
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workspace)
            if self._ignored(rel) or path == self.db_path:
                continue
            raw_path = rel.as_posix()
            disk_paths.add(raw_path)
            if self.update_file(path):
                updated += 1

        removed = 0
        with self._connect() as db:
            indexed = {str(row["path"]) for row in db.execute("SELECT path FROM files")}
            stale = sorted(indexed - disk_paths)
            for raw_path in stale:
                db.execute("DELETE FROM files WHERE path = ?", (raw_path,))
            removed = len(stale)
        stats = self.stats()
        return RegistryStats(stats.files, stats.symbols, stats.relations, updated, removed)

    def update_file(self, path: str | Path) -> bool:
        resolved = Path(path).resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ValueError(f"Path escapes workspace: {path}")
        rel = resolved.relative_to(self.workspace)
        raw_path = rel.as_posix()
        if self._ignored(rel):
            return False
        if not resolved.is_file():
            with self._connect() as db:
                cursor = db.execute("DELETE FROM files WHERE path = ?", (raw_path,))
            return bool(cursor.rowcount)

        stat = resolved.stat()
        with self._connect() as db:
            current = db.execute(
                "SELECT size, mtime_ns FROM files WHERE path = ?",
                (raw_path,),
            ).fetchone()
        if current and int(current["size"]) == stat.st_size and int(current["mtime_ns"]) == stat.st_mtime_ns:
            return False

        data = resolved.read_bytes()
        text = ""
        if resolved.suffix.lower() in TEXT_SUFFIXES and len(data) <= MAX_SYMBOL_BYTES:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
        canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8") if text else data
        digest = hashlib.sha256(canonical).hexdigest()
        symbols, relations = self._extract(resolved.suffix.lower(), text)
        language = self._language(resolved.suffix.lower())
        line_count = len(text.splitlines()) if text else 0
        with self._connect() as db:
            db.execute(
                "INSERT INTO files(path, language, suffix, size, mtime_ns, content_hash, line_count, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET language=excluded.language, suffix=excluded.suffix, "
                "size=excluded.size, mtime_ns=excluded.mtime_ns, content_hash=excluded.content_hash, "
                "line_count=excluded.line_count, indexed_at=excluded.indexed_at",
                (raw_path, language, resolved.suffix.lower(), stat.st_size, stat.st_mtime_ns, digest, line_count, self._now()),
            )
            db.execute("DELETE FROM symbols WHERE file_path = ?", (raw_path,))
            db.execute("DELETE FROM relations WHERE source_path = ?", (raw_path,))
            db.executemany(
                "INSERT INTO symbols(file_path, name, kind, signature, start_line, end_line) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        raw_path,
                        symbol["name"],
                        symbol["kind"],
                        symbol["signature"],
                        symbol["start_line"],
                        symbol["end_line"],
                    )
                    for symbol in symbols
                ],
            )
            db.executemany(
                "INSERT INTO relations(source_path, target, kind, line) VALUES (?, ?, ?, ?)",
                [(raw_path, relation["target"], relation["kind"], relation["line"]) for relation in relations],
            )
        return True

    def file_hash(self, raw_path: str, refresh: bool = True) -> str:
        if refresh:
            self.update_file((self.workspace / raw_path).resolve())
        with self._connect() as db:
            row = db.execute("SELECT content_hash FROM files WHERE path = ?", (Path(raw_path).as_posix(),)).fetchone()
        return str(row["content_hash"]) if row else ""

    def stats(self) -> RegistryStats:
        with self._connect() as db:
            files = int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            symbols = int(db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
            relations = int(db.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
        return RegistryStats(files, symbols, relations)

    def search(self, query: str, limit: int = 40, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            self.sync()
        terms = self._terms(query)
        with self._connect() as db:
            files = [dict(row) for row in db.execute("SELECT * FROM files ORDER BY path LIMIT 5000")]
            symbols = [dict(row) for row in db.execute("SELECT * FROM symbols ORDER BY file_path, start_line")]
            relations = [dict(row) for row in db.execute("SELECT * FROM relations ORDER BY source_path, line")]
        by_file_symbols: dict[str, list[dict[str, Any]]] = {}
        by_file_relations: dict[str, list[dict[str, Any]]] = {}
        for symbol in symbols:
            by_file_symbols.setdefault(str(symbol["file_path"]), []).append(symbol)
        for relation in relations:
            by_file_relations.setdefault(str(relation["source_path"]), []).append(relation)

        results: list[dict[str, Any]] = []
        for file_row in files:
            raw_path = str(file_row["path"])
            file_symbols = by_file_symbols.get(raw_path, [])
            file_relations = by_file_relations.get(raw_path, [])
            path_text = raw_path.lower()
            symbol_text = " ".join(f"{item['name']} {item['signature']}" for item in file_symbols).lower()
            relation_text = " ".join(f"{item['kind']} {item['target']}" for item in file_relations).lower()
            score = 0
            for term in terms:
                score += 6 if term in path_text else 0
                score += 3 if term in symbol_text else 0
                score += 2 if term in relation_text else 0
            if not terms:
                score = 1 if file_row["suffix"] in SOURCE_SUFFIXES else 0
            results.append(
                {
                    **file_row,
                    "score": score,
                    "symbols": file_symbols,
                    "relations": file_relations,
                }
            )
        results.sort(key=lambda item: (-int(item["score"]), str(item["path"]).lower()))
        return results[: max(1, min(int(limit), 500))]

    def build_map(self, query: str = "", max_chars: int = 6000) -> str:
        sync_stats = self.sync()
        rows = self.search(query, limit=500, refresh=False)
        if not rows:
            return "Repository registry: workspace is empty."
        output = (
            f"Repository registry map ({sync_stats.files} files, {sync_stats.symbols} symbols, "
            f"{sync_stats.relations} relations; {sync_stats.updated} updated):"
        )
        omitted = 0
        for row in rows:
            entry = self._format_entry(row)
            candidate = f"{output}\n{entry}"
            if len(candidate) > max(400, max_chars):
                omitted += 1
                continue
            output = candidate
        if omitted:
            output += f"\n... {omitted} lower-priority registry entries omitted by map budget"
        return output

    @staticmethod
    def _format_entry(row: dict[str, Any]) -> str:
        first = f"{row['path']} ({row['size']} B, sha256 {str(row['content_hash'])[:12]})"
        details: list[str] = []
        for symbol in row["symbols"][:24]:
            signature = str(symbol["signature"] or symbol["name"])
            details.append(
                f"  {symbol['kind']} {signature} (lines {symbol['start_line']}-{symbol['end_line']})"
            )
        for relation in row["relations"][:12]:
            details.append(f"  {relation['kind']} -> {relation['target']} (line {relation['line']})")
        return "\n".join([first, *details])

    @staticmethod
    def _ignored(rel: Path) -> bool:
        return any(part in IGNORED_PARTS for part in rel.parts)

    @staticmethod
    def _language(suffix: str) -> str:
        return {
            ".py": "Python",
            ".cpp": "C++",
            ".cc": "C++",
            ".cxx": "C++",
            ".h": "C++ Header",
            ".hpp": "C++ Header",
            ".js": "JavaScript",
            ".jsx": "JavaScript",
            ".ts": "TypeScript",
            ".tsx": "TypeScript",
            ".html": "HTML",
            ".css": "CSS",
            ".json": "JSON",
            ".md": "Markdown",
        }.get(suffix, "Text")

    @staticmethod
    def _terms(query: str) -> set[str]:
        return {
            term.lower()
            for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}", query)
        }

    def _extract(self, suffix: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not text or suffix not in SOURCE_SUFFIXES:
            return [], []
        if suffix == ".py":
            return self._extract_python(text)
        if suffix in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
            return self._extract_patterns(
                text,
                [
                    (r"^\s*(?:class|struct)\s+([A-Za-z_]\w*)", "type"),
                    (
                        r"^\s*(?:[\w:<>,~*&]+\s+)+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?\{?\s*$",
                        "function",
                    ),
                ],
                [(r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", "include")],
            )
        return self._extract_patterns(
            text,
            [
                (r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
                (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
                (r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=.*=>", "function"),
            ],
            [
                (r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", "import"),
                (r"^\s*(?:const|let|var)\s+\w+\s*=\s*require\(['\"]([^'\"]+)['\"]\)", "require"),
            ],
        )

    @staticmethod
    def _extract_python(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [
                {
                    "name": "<syntax-error>",
                    "kind": "syntax",
                    "signature": "invalid Python (AST unavailable)",
                    "start_line": 1,
                    "end_line": 1,
                }
            ], []

        symbols: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(CodeRegistry._python_symbol(node, "function", node.name))
            elif isinstance(node, ast.ClassDef):
                symbols.append(
                    {
                        "name": node.name,
                        "kind": "class",
                        "signature": node.name,
                        "start_line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                    }
                )
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(CodeRegistry._python_symbol(child, "method", f"{node.name}.{child.name}"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    relations.append({"target": alias.name, "kind": "import", "line": node.lineno})
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
                relations.append({"target": target, "kind": "import", "line": node.lineno})
            elif isinstance(node, ast.Call):
                name = CodeRegistry._call_name(node.func)
                if name:
                    relations.append({"target": name, "kind": "call", "line": node.lineno})
        return symbols, relations[:500]

    @staticmethod
    def _python_symbol(node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str, name: str) -> dict[str, Any]:
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = "..."
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return {
            "name": name,
            "kind": kind,
            "signature": f"{prefix}{name}({args})",
            "start_line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
        }

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts = [node.attr]
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
            return ".".join(reversed(parts))
        return ""

    @staticmethod
    def _extract_patterns(
        text: str,
        symbol_patterns: list[tuple[str, str]],
        relation_patterns: list[tuple[str, str]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        symbols: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern, kind in symbol_patterns:
                match = re.match(pattern, line)
                if match:
                    name = match.group(1)
                    symbols.append(
                        {
                            "name": name,
                            "kind": kind,
                            "signature": name,
                            "start_line": line_number,
                            "end_line": line_number,
                        }
                    )
                    break
            for pattern, kind in relation_patterns:
                match = re.match(pattern, line)
                if match:
                    relations.append({"target": match.group(1), "kind": kind, "line": line_number})
                    break
        return symbols, relations
