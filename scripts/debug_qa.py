"""Isolated QA activity debug runner.

Runs the QA stage (lint, typecheck, pytest, LLM summary) against a single
artifact without Temporal.

Usage:
    # Point at the artifact produced by a previous debug_dev run:
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free \\
        ARTIFACT=/tmp/ai-factory-debug/projects/calclib/calclib/calc.py \\
        .venv/bin/python scripts/debug_qa.py

    # Use built-in defaults (expects calclib project to exist in debug workspace):
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free \\
        .venv/bin/python scripts/debug_qa.py

Env vars:
    LLM_MODEL           model (default: minimax/MiniMax-M2.5-Free)
    LLM_PROVIDER        provider (default: opencode)
    ARTIFACT            path to the artifact file to validate
    GITHUB_URL          repo URL (default: https://github.com/alexeyban/calclib)
    PROJECT_NAME        (default: calclib)
    PROJECTS_ROOT       (default: /tmp/ai-factory-debug/projects)
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

from orchestrator.activities import qa_activity  # noqa: E402
from shared.git import branch_exists, ensure_branch, run_git
from orchestrator.activities import _task_branch, _project_repo_path  # noqa: E402

GITHUB_URL   = os.getenv("GITHUB_URL",   "https://github.com/alexeyban/calclib")
PROJECT_NAME = os.getenv("PROJECT_NAME", "calclib")
ARTIFACT     = os.getenv(
    "ARTIFACT",
    "/tmp/ai-factory-debug/projects/calclib/calclib/calc.py",
)


async def main():
    model    = os.getenv("LLM_MODEL")
    provider = os.getenv("LLM_PROVIDER")

    if not Path(ARTIFACT).exists():
        print(f"⚠  Artifact not found: {ARTIFACT}")
        print("Run debug_dev.py first or set ARTIFACT= to an existing file.")
        return 1

    workflow_id = f"debug-qa-{int(time.time())}"
    task = {
        "task_id":           "T003",
        "title":             "Guard divide() against division-by-zero in calclib/calc.py",
        "description": (
            "In calclib/calc.py, add a guard to the divide() function so it raises "
            "ValueError when the divisor is zero. "
            "Ensure tests/test_calc.py has a test_divide_by_zero test."
        ),
        "type":              "bugfix",
        "assigned_agent":    "dev",
        "dependencies":      [],
        "acceptance_criteria": [
            "divide(1, 0) raises ValueError",
            "pytest tests pass",
        ],
        "estimated_size":    "small",
        "project_name":      PROJECT_NAME,
        "github_url":        GITHUB_URL,
        "_workflow_id":      workflow_id,
        "artifact":          ARTIFACT,
        "attempt_number":    1,
        "verification": {
            "method": "pytest",
            "test_file": "tests/test_calc.py",
            "criteria": ["test_divide_by_zero passes"],
        },
    }

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"QA isolation test")
    print(f"  model      : {model}")
    print(f"  provider   : {provider}")
    print(f"  project    : {PROJECT_NAME}  ({GITHUB_URL})")
    print(f"  artifact   : {ARTIFACT}")
    print(f"  workflow   : {workflow_id}")
    print(sep)

    t0 = time.time()
    result = await qa_activity(task)
    elapsed = time.time() - t0

    context_file = result.get("_context_file")
    if context_file and Path(context_file).exists():
        full = json.loads(Path(context_file).read_text())
        full.pop("_meta", None)
    else:
        full = result

    status   = full.get("status", "?")
    logs     = full.get("logs", "")
    summary  = full.get("summary", {})

    print(f"\n{sep}")
    print(f"QA completed in {elapsed:.1f}s   status={status}")
    print(f"  feedback     : {full.get('feedback', 'N/A')}")
    if summary:
        print(f"  error_summary: {summary.get('error_summary', 'N/A')[:120]}")
        print(f"  fix_suggestion: {summary.get('fix_suggestion', 'N/A')[:120]}")
    print()
    print("Logs (last 60 lines):")
    for line in logs.splitlines()[-60:]:
        print(f"  {line}")

    rc = 0 if status == "success" else 1
    print()
    if rc:
        print("  ⚠  QA did NOT pass")
    else:
        print("  ✓ QA passed")

    print()
    print("Full result keys:", list(full.keys()))
    if context_file:
        print(f"Context file   : {context_file}")

    out = Path(f"/tmp/debug_qa_{workflow_id}.json")
    out.write_text(json.dumps(full, indent=2, ensure_ascii=False))
    print(f"Full JSON dump : {out}")
    print(sep)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
