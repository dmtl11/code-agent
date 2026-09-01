from __future__ import annotations

from pathlib import Path

from .code_registry import CodeRegistry


class RepoMap:
    """Build a compact, deterministic map of files and important symbols."""

    def __init__(self, workspace: str | Path, registry: CodeRegistry | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.registry = registry or CodeRegistry(self.workspace)

    def build(self, query: str = "", max_chars: int = 6000) -> str:
        return self.registry.build_map(query, max_chars)
