# Code Agent Evaluation

## What To Measure

1. End-to-end task success
   The main score is whether the agent actually completes the requested programming task in a fresh workspace. This is closer to SWE-bench than plain code completion because the agent must inspect files, edit code, and run checks.

2. Test and verification pass rate
   Every case should have an executable check. For algorithm tasks this can be unit tests or sample tests; for repo tasks it can be the existing test command. Pass rate is the headline metric.

3. Localization and context use
   A good coding agent should find the relevant files without reading everything blindly. Useful signals include search calls, line-window reads, number of files touched, and whether unrelated files were left unchanged.

4. Error recovery
   The agent should react when commands fail. Examples: retry `python` after `python3` fails on Windows, inspect a traceback, or patch a syntax error.

5. Safety and containment
   File access should stay inside the workspace. Destructive commands should be blocked or require approval. Secrets should come from ignored environment files rather than source code.

6. Efficiency
   Track wall time, model turns, tool calls, command count, and token/cost estimates where available. A strong agent should not need excessive loops for small tasks.

7. Code quality
   Passing tests is not enough. Review naming, readability, minimality of changes, edge cases, and whether the solution matches existing style.

8. Reproducibility
   Evaluation cases should seed a clean workspace, run the same task, execute the same checks, and write a machine-readable report.

## Local Suite

Run:

```powershell
python run.py --eval
```

The suite reads `benchmarks/local_eval.json`, creates `eval_runs/`, runs the real LLM-backed agent for each case, executes checks, and writes `eval_runs/report.json`.

The initial cases cover:

- creating a small module and verifying it
- fixing a seeded bug until tests pass
- adding a file while preserving an unrelated file

## Current Project Improvements

- Added `search_files` so the agent can localize code before editing.
- Added line-window `read_file` behavior to avoid dumping huge files into context. By default it returns a numbered window and tells the agent where to continue.
- Added `lint_file` for Python and C++ syntax checks, and updated the system prompt to use it before and after edits when possible.
- Added Python syntax validation before `write_file` commits `.py` content.
- Added explicit empty-output reporting for successful commands.
- Added a reproducible local eval runner with per-case events and report JSON.
