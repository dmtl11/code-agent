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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Run the coding-agent harness.")
    parser.add_argument("task", nargs="?", default="Create a hello world Python script and run it.")
    parser.add_argument("--workspace", default="workspace", help="Directory the agent may read and write.")
    parser.add_argument("--max-turns", type=int, default=12)
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    final = CodingAgent(workspace, args.max_turns, print_event).run(args.task)
    print(f"\nfinal: {final}")


if __name__ == "__main__":
    main()
