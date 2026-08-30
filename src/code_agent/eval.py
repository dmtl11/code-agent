from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import CodingAgent


ROOT = Path(__file__).resolve().parents[2]


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    duration_seconds: float
    tool_calls: int
    checks: list[dict[str, Any]]
    final: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a small local evaluation suite for the coding agent.")
    parser.add_argument("--cases", default=str(ROOT / "benchmarks" / "local_eval.json"))
    parser.add_argument("--workspace-root", default=str(ROOT / "eval_runs"))
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--keep", action="store_true", help="Keep old eval workspace contents.")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    workspace_root = Path(args.workspace_root).resolve()
    if workspace_root != ROOT and ROOT not in workspace_root.parents:
        raise SystemExit(f"Refusing to clear eval workspace outside project root: {workspace_root}")
    if workspace_root.exists() and not args.keep:
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    results = [run_case(case, workspace_root, args.max_turns) for case in cases]
    passed = sum(1 for result in results if result.passed)
    report = {
        "passed": passed,
        "total": len(results),
        "pass_rate": passed / len(results) if results else 0,
        "results": [result.__dict__ for result in results],
    }
    output = workspace_root / "report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {output}")


def run_case(case: dict[str, Any], workspace_root: Path, max_turns: int) -> EvalResult:
    workspace = workspace_root / case["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    for rel_path, content in case.get("seed_files", {}).items():
        path = workspace / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    events: list[dict[str, Any]] = []
    start = time.perf_counter()
    try:
        final = CodingAgent(workspace, max_turns=max_turns, sink=events.append).run(case["task"])
    except Exception as exc:
        final = f"{type(exc).__name__}: {exc}"
        events.append({"type": "error", "message": final})
    duration = time.perf_counter() - start

    checks = [run_check(workspace, check) for check in case.get("checks", [])]
    passed = checks and all(check["ok"] for check in checks)
    tool_calls = sum(1 for event in events if event.get("type") == "tool_call")
    (workspace / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    return EvalResult(
        case_id=case["id"],
        passed=passed,
        duration_seconds=round(duration, 3),
        tool_calls=tool_calls,
        checks=checks,
        final=final,
    )


def run_check(workspace: Path, check: dict[str, Any]) -> dict[str, Any]:
    check_type = check["type"]
    if check_type == "file_exists":
        path = workspace / check["path"]
        return {"type": check_type, "ok": path.is_file(), "path": check["path"]}
    if check_type == "file_contains":
        path = workspace / check["path"]
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {"type": check_type, "ok": check["text"] in text, "path": check["path"]}
    if check_type == "command":
        completed = subprocess.run(
            check["command"],
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=int(check.get("timeout", 20)),
        )
        return {
            "type": check_type,
            "ok": completed.returncode == 0,
            "command": check["command"],
            "exit_code": completed.returncode,
            "output": (completed.stdout + completed.stderr)[-4000:],
        }
    raise ValueError(f"Unknown check type: {check_type}")


if __name__ == "__main__":
    main()
