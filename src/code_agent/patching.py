from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class PatchError(RuntimeError):
    """Raised when a patch is malformed, stale, or cannot be applied atomically."""


@dataclass(frozen=True)
class PatchOperation:
    kind: str
    path: str
    body: tuple[str, ...]


@dataclass(frozen=True)
class FileChange:
    path: str
    before_exists: bool
    before_content: str
    after_exists: bool
    after_content: str


@dataclass(frozen=True)
class PatchResult:
    transaction_id: str
    paths: tuple[str, ...]
    diff: str
    dry_run: bool = False

    def message(self) -> str:
        label = "Dry run succeeded" if self.dry_run else f"Applied patch {self.transaction_id}"
        paths = ", ".join(self.paths)
        return f"{label} for: {paths}\n\n{self.diff}".rstrip()


class PatchEngine:
    """Apply exact-context multi-file patches with conflict checks and rollback journals."""

    def __init__(
        self,
        workspace: str | Path,
        validate_candidate: Callable[[str, str], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.validate_candidate = validate_candidate or (lambda _path, _content: None)
        self.journal_dir = self.workspace / ".code_agent" / "patches"
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def apply(
        self,
        patch_text: str,
        dry_run: bool = False,
        base_hashes: dict[str, str] | None = None,
    ) -> PatchResult:
        operations = self.parse(patch_text)
        changes: list[FileChange] = []
        seen: set[str] = set()
        normalized_hashes = {self._normalize_path(path): str(value) for path, value in (base_hashes or {}).items()}
        for operation in operations:
            raw_path = self._normalize_path(operation.path)
            if raw_path in seen:
                raise PatchError(f"Patch contains more than one operation for {raw_path}")
            seen.add(raw_path)
            path = self._resolve(raw_path)
            before_exists = path.is_file()
            before = path.read_text(encoding="utf-8") if before_exists else ""
            expected_hash = normalized_hashes.get(raw_path, "").strip().lower()
            actual_hash = self.hash_content(before) if before_exists else ""
            if expected_hash and not actual_hash.startswith(expected_hash):
                raise PatchError(
                    f"Conflict in {raw_path}: expected base hash {expected_hash}, found {actual_hash[:12] or '<missing>'}."
                )

            if operation.kind == "add":
                if before_exists:
                    raise PatchError(f"Conflict in {raw_path}: Add File requires a path that does not exist.")
                after = self._add_content(operation.body)
                after_exists = True
            elif operation.kind == "delete":
                if not before_exists:
                    raise PatchError(f"Conflict in {raw_path}: Delete File requires an existing file.")
                after = ""
                after_exists = False
            else:
                if not before_exists:
                    raise PatchError(f"Conflict in {raw_path}: Update File requires an existing file.")
                after = self._apply_update(raw_path, before, operation.body)
                after_exists = True

            if after_exists:
                self.validate_candidate(raw_path, after)
            changes.append(FileChange(raw_path, before_exists, before, after_exists, after))

        diff = self._combined_diff(changes)
        if dry_run:
            return PatchResult("", tuple(change.path for change in changes), diff, True)

        transaction_id = f"patch_{uuid.uuid4().hex[:16]}"
        try:
            self._write_changes(changes, use_after=True)
            self._write_journal(transaction_id, patch_text, changes, "applied")
        except Exception as exc:
            self._write_changes(changes, use_after=False)
            if isinstance(exc, PatchError):
                raise
            raise PatchError(f"Patch write failed and was rolled back: {exc}") from exc
        return PatchResult(transaction_id, tuple(change.path for change in changes), diff)

    def rollback(self, transaction_id: str) -> PatchResult:
        if not re.fullmatch(r"patch_[A-Za-z0-9]{8,64}", transaction_id):
            raise PatchError("Invalid patch transaction id")
        journal_path = self.journal_dir / f"{transaction_id}.json"
        if not journal_path.is_file():
            raise PatchError(f"Unknown patch transaction: {transaction_id}")
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        if payload.get("status") != "applied":
            raise PatchError(f"Patch {transaction_id} has status {payload.get('status', 'unknown')}")
        changes = [FileChange(**item) for item in payload.get("changes", [])]
        for change in changes:
            path = self._resolve(change.path)
            current_exists = path.is_file()
            if current_exists != change.after_exists:
                raise PatchError(f"Rollback conflict in {change.path}: file existence changed after the patch.")
            if current_exists:
                current = path.read_text(encoding="utf-8")
                if self.hash_content(current) != self.hash_content(change.after_content):
                    raise PatchError(f"Rollback conflict in {change.path}: file content changed after the patch.")

        reverse = [
            FileChange(
                path=change.path,
                before_exists=change.after_exists,
                before_content=change.after_content,
                after_exists=change.before_exists,
                after_content=change.before_content,
            )
            for change in changes
        ]
        try:
            self._write_changes(reverse, use_after=True)
        except Exception as exc:
            self._write_changes(reverse, use_after=False)
            raise PatchError(f"Rollback failed and the patch state was restored: {exc}") from exc
        payload["status"] = "rolled_back"
        payload["rolled_back_at"] = self._now()
        journal_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return PatchResult(transaction_id, tuple(change.path for change in changes), self._combined_diff(reverse))

    @staticmethod
    def parse(patch_text: str) -> list[PatchOperation]:
        lines = patch_text.strip().splitlines()
        if len(lines) < 3 or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
            raise PatchError("Patch must start with '*** Begin Patch' and end with '*** End Patch'.")
        operations: list[PatchOperation] = []
        index = 1
        while index < len(lines) - 1:
            header = lines[index]
            match = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", header)
            if not match:
                raise PatchError(f"Expected file operation at patch line {index + 1}: {header}")
            kind = match.group(1).lower()
            raw_path = match.group(2).strip()
            index += 1
            body: list[str] = []
            while index < len(lines) - 1 and not re.fullmatch(
                r"\*\*\* (?:Add|Update|Delete) File: .+",
                lines[index],
            ):
                body.append(lines[index])
                index += 1
            operations.append(PatchOperation(kind, raw_path, tuple(body)))
        if not operations:
            raise PatchError("Patch contains no file operations.")
        return operations

    def _apply_update(self, raw_path: str, content: str, body: tuple[str, ...]) -> str:
        hunks: list[list[str]] = []
        current: list[str] | None = None
        for line in body:
            if line.startswith("@@"):
                if current is not None:
                    hunks.append(current)
                current = []
                continue
            if current is None:
                if not line.strip():
                    continue
                raise PatchError(f"Update File {raw_path} requires an @@ hunk before changed lines.")
            current.append(line)
        if current is not None:
            hunks.append(current)
        if not hunks:
            raise PatchError(f"Update File {raw_path} contains no hunks.")

        trailing_newline = content.endswith("\n")
        candidate = content.splitlines()
        for hunk_number, hunk in enumerate(hunks, start=1):
            old_lines: list[str] = []
            new_lines: list[str] = []
            for line in hunk:
                if not line:
                    prefix, value = " ", ""
                else:
                    prefix, value = line[0], line[1:]
                if prefix not in {" ", "+", "-"}:
                    raise PatchError(f"Invalid hunk line in {raw_path}: {line}")
                if prefix in {" ", "-"}:
                    old_lines.append(value)
                if prefix in {" ", "+"}:
                    new_lines.append(value)
            if not old_lines:
                raise PatchError(f"Hunk {hunk_number} in {raw_path} needs at least one context or removed line.")
            positions = [
                position
                for position in range(0, len(candidate) - len(old_lines) + 1)
                if candidate[position : position + len(old_lines)] == old_lines
            ]
            if len(positions) != 1:
                raise PatchError(
                    f"Conflict in {raw_path} hunk {hunk_number}: expected one exact context match, found {len(positions)}."
                )
            position = positions[0]
            candidate[position : position + len(old_lines)] = new_lines
        output = "\n".join(candidate)
        if trailing_newline:
            output += "\n"
        return output

    @staticmethod
    def _add_content(body: tuple[str, ...]) -> str:
        lines: list[str] = []
        for line in body:
            if line.startswith("+"):
                lines.append(line[1:])
            elif not line:
                lines.append("")
            else:
                raise PatchError("Add File content lines must start with '+'.")
        return "\n".join(lines) + ("\n" if lines else "")

    def _write_changes(self, changes: list[FileChange], use_after: bool) -> None:
        for change in changes:
            path = self._resolve(change.path)
            exists = change.after_exists if use_after else change.before_exists
            content = change.after_content if use_after else change.before_content
            if exists:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            elif path.exists():
                path.unlink()

    def _write_journal(
        self,
        transaction_id: str,
        patch_text: str,
        changes: list[FileChange],
        status: str,
    ) -> None:
        payload = {
            "id": transaction_id,
            "status": status,
            "created_at": self._now(),
            "patch": patch_text,
            "changes": [change.__dict__ for change in changes],
        }
        path = self.journal_dir / f"{transaction_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _resolve(self, raw_path: str) -> Path:
        path = (self.workspace / raw_path).resolve()
        if path == self.workspace or self.workspace not in path.parents:
            raise PatchError(f"Path escapes workspace or is not a file path: {raw_path}")
        return path

    @staticmethod
    def _normalize_path(raw_path: str) -> str:
        value = str(raw_path).replace("\\", "/").strip()
        while value.startswith("./"):
            value = value[2:]
        if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            raise PatchError(f"Patch path must be relative: {raw_path}")
        return Path(value).as_posix()

    @staticmethod
    def hash_content(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _combined_diff(changes: list[FileChange]) -> str:
        sections: list[str] = []
        for change in changes:
            before = change.before_content.splitlines()
            after = change.after_content.splitlines()
            from_file = f"a/{change.path}" if change.before_exists else "/dev/null"
            to_file = f"b/{change.path}" if change.after_exists else "/dev/null"
            lines = difflib.unified_diff(before, after, fromfile=from_file, tofile=to_file, lineterm="")
            sections.append("\n".join(lines))
        return "\n".join(section for section in sections if section)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
