"""Isolated Architect activity debug runner.

Feeds the architect either a fresh description or the saved PM output from a
previous debug_pm run, then prints the task breakdown.

Usage:
    # Use built-in calclib description directly:
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free .venv/bin/python scripts/debug_architect.py

    # Feed it the PM output JSON from a previous debug_pm run:
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free PM_OUTPUT=/tmp/debug_pm_<id>.json \
        .venv/bin/python scripts/debug_architect.py

Env vars:
    LLM_MODEL        model (default: minimax/MiniMax-M2.5-Free)
    LLM_PROVIDER     provider (default: opencode)
    PM_OUTPUT        path to a debug_pm JSON dump to use as input
    GITHUB_URL       repo URL (default: https://github.com/alexeyban/calclib)
    PROJECT_NAME     (default: calclib)
    PROJECTS_ROOT    local workspace (default: /tmp/ai-factory-debug/projects)
    WORKSPACE_ROOT   (default: /tmp/ai-factory-debug)
    AI_FACTORY_ROOT  (default: /tmp/ai-factory-debug/.ai_factory)
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

from orchestrator.activities import architect_activity  # noqa: E402

GITHUB_URL   = os.getenv("GITHUB_URL",   "https://github.com/alexeyban/calclib")
PROJECT_NAME = os.getenv("PROJECT_NAME", "calclib")

_FALLBACK_DESCRIPTION = """\
Improve the calclib Python library at https://github.com/alexeyban/calclib.

calclib is a minimal Python calculator library with:
- calclib/calc.py: add, subtract, multiply, divide functions
- tests/test_calc.py: pytest tests

Tasks:
1. Ensure divide raises ValueError on division by zero (add guard if missing)
2. Add a power(base, exp) function to calclib/calc.py with tests
3. Ensure README.md exists with a short description

All work goes into the https://github.com/alexeyban/calclib repository.
"""


def _build_description_from_pm(pm_path: str) -> str:
    """Build the architect input description from a PM output JSON dump."""
    data = json.loads(Path(pm_path).read_text())
    base_desc = data.get("project_goal", _FALLBACK_DESCRIPTION)
    summary   = data.get("delivery_summary", "")
    guidance  = data.get("architect_guidance", [])
    plan      = data.get("execution_plan", [])
    titles    = "\n".join(
        f"- [{t.get('assigned_agent','dev')}] {t.get('title', t.get('description',''))[:80]}"
        for t in plan[:30]
    )
    return (
        f"{base_desc}\n\n"
        f"PM delivery summary:\n{summary}\n\n"
        f"PM architect guidance:\n{guidance}\n\n"
        f"PM execution plan tasks:\n{titles}"
    )


async def main():
    model    = os.getenv("LLM_MODEL")
    provider = os.getenv("LLM_PROVIDER")
    pm_path  = os.getenv("PM_OUTPUT")

    if pm_path:
        print(f"Using PM output from: {pm_path}")
        description = _build_description_from_pm(pm_path)
    else:
        description = _FALLBACK_DESCRIPTION

    workflow_id = f"debug-architect-{int(time.time())}"
    task = {
        "task_id":      workflow_id,
        "description":  description,
        "project_name": PROJECT_NAME,
        "github_url":   GITHUB_URL,
        "_workflow_id": workflow_id,
    }

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"Architect isolation test")
    print(f"  model      : {model}")
    print(f"  provider   : {provider}")
    print(f"  project    : {PROJECT_NAME}  ({GITHUB_URL})")
    print(f"  desc chars : {len(description)}")
    print(f"  workflow   : {workflow_id}")
    print(sep)

    t0 = time.time()
    result = await architect_activity(task)
    elapsed = time.time() - t0

    # Load full result from context file if slim envelope was returned
    context_file = result.get("_context_file")
    if context_file and Path(context_file).exists():
        full = json.loads(Path(context_file).read_text())
        full.pop("_meta", None)
    else:
        full = result

    tasks  = full.get("tasks", [])
    status = full.get("status", "?")

    print(f"\n{sep}")
    print(f"Architect completed in {elapsed:.1f}s   status={status}")
    print(f"Tasks defined : {len(tasks)}")
    print()

    if not tasks:
        print("⚠  NO TASKS returned from architect!")
        rc = 1
    else:
        rc = 0
        for i, t in enumerate(tasks, 1):
            tid   = t.get("task_id", f"T{i:03d}")
            ttype = t.get("type", "?")
            agent = t.get("assigned_agent", "?")
            title = t.get("title") or t.get("description", "")[:80]
            deps  = t.get("dependencies", [])
            tokens = t.get("_estimated_tokens", "?")
            dep_str = f"  deps={deps}" if deps else ""
            print(f"  {tid} [{ttype}/{agent}] {title}{dep_str}")

        # Sanity checks
        print()
        missing_type  = [t.get("task_id") for t in tasks if not t.get("type")]
        missing_title = [t.get("task_id") for t in tasks if not t.get("title")]
        if missing_type:
            print(f"  ⚠  Missing 'type'  : {missing_type}")
            rc = 1
        if missing_title:
            print(f"  ⚠  Missing 'title' : {missing_title}")
            rc = 1
        if not missing_type and not missing_title:
            print("  ✓ All tasks have type and title")

    print()
    print("Full result keys:", list(full.keys()))
    if context_file:
        print(f"Context file   : {context_file}")

    out = Path(f"/tmp/debug_architect_{workflow_id}.json")
    out.write_text(json.dumps(full, indent=2, ensure_ascii=False))
    print(f"Full JSON dump : {out}")
    print(sep)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
