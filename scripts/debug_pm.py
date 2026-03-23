"""Isolated PM activity debug runner.

Calls pm_activity directly (no Temporal) with a real LLM call so you can
inspect prompt → LLM → plan output without the full pipeline.

Usage:
    PYTHONPATH=. LLM_MODEL=opencode/bigpickle .venv/bin/python scripts/debug_pm.py

Options (env vars):
    LLM_MODEL        model to use (default: opencode/bigpickle)
    LLM_PROVIDER     override provider (inferred from model if not set)
    GITHUB_URL       target repo (default: https://github.com/alexeyban/calclib)
    PROJECT_NAME     project name (default: calclib)
    DESCRIPTION      task description (default: built-in calclib description)
    MOCK_LLM         set to 'true' to skip real LLM calls
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

# Force the model before importing activities (activities reads env at import time for prompts)
model = os.environ.setdefault("LLM_MODEL", "opencode/bigpickle")
provider = os.environ.setdefault("LLM_PROVIDER", "opencode")

from orchestrator.activities import pm_activity  # noqa: E402

GITHUB_URL   = os.getenv("GITHUB_URL", "https://github.com/alexeyban/calclib")
PROJECT_NAME = os.getenv("PROJECT_NAME", "calclib")
DESCRIPTION  = os.getenv("DESCRIPTION", """\
Improve the calclib Python library at https://github.com/alexeyban/calclib.

calclib is a minimal Python calculator library with:
- calclib/calc.py: add, subtract, multiply, divide functions
- tests/test_calc.py: pytest tests

Tasks:
1. Ensure divide raises ValueError on division by zero (add guard if missing)
2. Add a power(base, exp) function to calclib/calc.py with tests
3. Ensure README.md exists with a short description

All work goes into the https://github.com/alexeyban/calclib repository.
""")


async def main():
    workflow_id = f"debug-pm-{int(time.time())}"
    task = {
        "task_id":       workflow_id,
        "description":   DESCRIPTION,
        "project_name":  PROJECT_NAME,
        "github_url":    GITHUB_URL,
        "_workflow_id":  workflow_id,
    }

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"PM isolation test")
    print(f"  model    : {model}")
    print(f"  provider : {provider}")
    print(f"  project  : {PROJECT_NAME}  ({GITHUB_URL})")
    print(f"  workflow : {workflow_id}")
    print(sep)

    t0 = time.time()
    result = await pm_activity(task)
    elapsed = time.time() - t0

    # pm_activity returns a slim envelope (with _context_file) when result is large
    context_file = result.get("_context_file")
    if context_file and Path(context_file).exists():
        full = json.loads(Path(context_file).read_text())
        full.pop("_meta", None)
    else:
        full = result

    plan    = full.get("execution_plan", [])
    summary = full.get("delivery_summary", "")
    status  = full.get("status", "?")

    print(f"\n{sep}")
    print(f"PM completed in {elapsed:.1f}s   status={status}")
    print(f"Tasks planned : {len(plan)}")
    print(f"Delivery summary:\n  {summary[:300]}")
    print()

    if not plan:
        print("⚠  NO TASKS returned — PM produced an empty execution plan!")
        rc = 1
    else:
        rc = 0
        for i, t in enumerate(plan, 1):
            agent = t.get("assigned_agent", "?")
            title = t.get("title") or t.get("description", "")[:80]
            deps  = t.get("dependencies", [])
            print(f"  {i:2}. [{agent}] {title}" + (f"  deps={deps}" if deps else ""))

    print()
    print("Full result keys:", list(full.keys()))
    if context_file:
        print(f"Context file   : {context_file}")

    # Dump full result for inspection
    out = Path(f"/tmp/debug_pm_{workflow_id}.json")
    out.write_text(json.dumps(full, indent=2, ensure_ascii=False))
    print(f"Full JSON dump : {out}")
    print(sep)

    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
