from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


if "--web" in sys.argv:
    sys.argv.remove("--web")
    from code_agent.server import main
elif "--eval" in sys.argv:
    sys.argv.remove("--eval")
    from code_agent.eval import main
else:
    from code_agent.cli import main


if __name__ == "__main__":
    main()
