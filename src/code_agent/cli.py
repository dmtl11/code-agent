from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import CodingAgent


def print_event(event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "tool_call":
        print(f"\n> tool: {event['name']} {json.dumps(event.get('arguments', {}), ensure_ascii=False)}")
    elif kind == "tool_result":
        status = "ok" if event.get("ok") else "error"
        print(f"< {event['name']} [{status}]\n{event.get('output', '')}")
    elif kind == "assistant":
        print(f"\nassistant: {event.get('content', '')}")
    elif kind == "step":
        print(f"\nstep {event.get('step')}: {event.get('message')}")
    elif kind == "context":
        compacted = int(event.get("compacted_blocks", 0))
        truncated = int(event.get("truncated_tool_results", 0))
        print(
            f"\ncontext: {event.get('estimated_tokens', 0)}/{event.get('max_tokens', 0)} tokens, "
            f"compacted={compacted}, truncated={truncated}"
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Run the coding-agent harness.")
    parser.add_argument("task", nargs="?", default="Create a hello world Python script and run it.")
    parser.add_argument("--workspace", default="workspace", help="Directory the agent may read and write.")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--mode", choices=["code", "ask", "architect", "context"], default="code")
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=None,
        help="Override the context budget for this run (minimum 4000).",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    final = CodingAgent(
        workspace,
        args.max_turns,
        print_event,
        mode=args.mode,
        context_tokens=args.context_tokens,
    ).run(args.task)
    print(f"\nfinal: {final}")


if __name__ == "__main__":
    main()
