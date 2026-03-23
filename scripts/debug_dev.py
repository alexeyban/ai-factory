"""Isolated Dev activity debug runner.

Runs a single task through the dev+QA self-healing loop without Temporal.

Usage:
    # Use built-in simple task (divide zero-guard):
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free .venv/bin/python scripts/debug_dev.py

    # Override task description:
    PYTHONPATH=. TASK_TITLE="Add power function" TASK_DESCRIPTION="..." \\
        .venv/bin/python scripts/debug_dev.py

Env vars:
    LLM_MODEL           model (default: minimax/MiniMax-M2.5-Free)
    LLM_PROVIDER        provider (default: opencode)
    GITHUB_URL          repo URL (default: https://github.com/alexeyban/calclib)
    PROJECT_NAME        (default: calclib)
    TASK_TITLE          override task title
    TASK_DESCRIPTION    override task description
    PROJECTS_ROOT       local workspace (default: /tmp/ai-factory-debug/projects)
    WORKSPACE_ROOT      (default: /tmp/ai-factory-debug)
    AI_FACTORY_ROOT     (default: /tmp/ai-factory-debug/.ai_factory)
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

os.environ.setdefault("LLM_MODEL", "minimax/MiniMax-M2.5-Free")
os.environ.setdefault("LLM_PROVIDER", "opencode")
os.environ.setdefault("PROJECTS_ROOT",   "/tmp/ai-factory-debug/projects")
os.environ.setdefault("WORKSPACE_ROOT",  "/tmp/ai-factory-debug")
os.environ.setdefault("AI_FACTORY_ROOT", "/tmp/ai-factory-debug/.ai_factory")

from orchestrator.activities import dev_task  # noqa: E402

GITHUB_URL   = os.getenv("GITHUB_URL",   "https://github.com/alexeyban/calclib")
PROJECT_NAME = os.getenv("PROJECT_NAME", "calclib")

TASK_TITLE = os.getenv(
    "TASK_TITLE",
    "Guard divide() against division-by-zero in calclib/calc.py",
)
TASK_DESCRIPTION = os.getenv(
    "TASK_DESCRIPTION",
    """\
In calclib/calc.py, add a guard to the divide() function so it raises
ValueError when the divisor is zero. The guard should be:
    if b == 0:
        raise ValueError("Cannot divide by zero")

Also ensure tests/test_calc.py has a test:
    def test_divide_by_zero():
        import pytest
        with pytest.raises(ValueError):
            divide(1, 0)

Commit the changes to the task branch.
""",
)


async def main():
    model    = os.getenv("LLM_MODEL")
    provider = os.getenv("LLM_PROVIDER")

    workflow_id = f"debug-dev-{int(time.time())}"
    task = {
        "task_id":           "T003",
        "title":             TASK_TITLE,
        "description":       TASK_DESCRIPTION,
        "type":              "bugfix",
        "assigned_agent":    "dev",
        "dependencies":      [],
        "acceptance_criteria": [
            "divide(1, 0) raises ValueError",
            "pytest tests pass",
        ],
        "estimated_size":    "small",
        "can_parallelize":   True,
        "project_name":      PROJECT_NAME,
        "github_url":        GITHUB_URL,
        "_workflow_id":      workflow_id,
        "input": {
            "files": ["calclib/calc.py", "tests/test_calc.py"],
            "context": "calclib is a minimal Python calculator library.",
        },
        "output": {
            "files": ["calclib/calc.py", "tests/test_calc.py"],
            "expected_result": "divide() raises ValueError on zero divisor; tests pass",
        },
        "verification": {
            "method": "pytest",
            "test_file": "tests/test_calc.py",
            "criteria": ["test_divide_by_zero passes"],
        },
    }

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"Dev isolation test")
    print(f"  model      : {model}")
    print(f"  provider   : {provider}")
    print(f"  project    : {PROJECT_NAME}  ({GITHUB_URL})")
    print(f"  task       : {task['task_id']} — {TASK_TITLE}")
    print(f"  workflow   : {workflow_id}")
    print(sep)

    t0 = time.time()
    result = await dev_task(task)
    elapsed = time.time() - t0

    # Load full result from context file if slim envelope was returned
    context_file = result.get("_context_file")
    if context_file and Path(context_file).exists():
        full = json.loads(Path(context_file).read_text())
        full.pop("_meta", None)
    else:
        full = result

    status = full.get("status", "?")
    qa     = full.get("qa", {})

    print(f"\n{sep}")
    print(f"Dev completed in {elapsed:.1f}s   status={status}")
    print(f"  qa_status  : {qa.get('status', 'N/A')}")
    print(f"  qa_verdict : {qa.get('verdict', 'N/A')}")
    print(f"  healing    : {full.get('self_healing_applied', False)}  "
          f"attempts={full.get('attempts', 0)}")
    if full.get("error"):
        print(f"  error      : {full['error']}")

    rc = 0 if status in {"success", "complete"} else 1
    if rc:
        print(f"  ⚠  Task did NOT succeed")
    else:
        print(f"  ✓ Task succeeded")

    print()
    print("Full result keys:", list(full.keys()))
    if context_file:
        print(f"Context file   : {context_file}")

    out = Path(f"/tmp/debug_dev_{workflow_id}.json")
    out.write_text(json.dumps(full, indent=2, ensure_ascii=False))
    print(f"Full JSON dump : {out}")
    print(sep)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
