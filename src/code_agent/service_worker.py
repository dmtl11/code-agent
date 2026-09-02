"""Gated launcher: user code starts only after the parent establishes ownership."""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    # Closing stdin before the gate opens must never start the user's command.
    if sys.stdin.buffer.read(1) != b"1":
        return 1
    command = json.loads(sys.argv[1])
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        return child.wait()
    except Exception as exc:
        print(f"Service launch failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
