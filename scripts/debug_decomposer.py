"""Isolated Decomposer activity debug runner.

Feeds the decomposer a task list from a previous architect debug run (or a
built-in minimal list) and prints the resulting decomposed tasks.

Usage:
    # Use built-in minimal task list:
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free .venv/bin/python scripts/debug_decomposer.py

    # Feed it the architect output JSON from a previous debug_architect run:
    PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free ARCHITECT_OUTPUT=/tmp/debug_architect_<id>.json \\
        .venv/bin/python scripts/debug_decomposer.py

Env vars:
    LLM_MODEL           model (default: minimax/MiniMax-M2.5-Free)
    LLM_PROVIDER        provider (default: opencode)
    ARCHITECT_OUTPUT    path to a debug_architect JSON dump to use as input
    GITHUB_URL          repo URL (default: https://github.com/alexeyban/calclib)
    PROJECT_NAME        (default: calclib)
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

from orchestrator.activities import decomposer_activity  # noqa: E402

GITHUB_URL   = os.getenv("GITHUB_URL",   "https://github.com/alexeyban/calclib")
PROJECT_NAME = os.getenv("PROJECT_NAME", "calclib")

_FALLBACK_TASKS = [
    {
        "task_id": "T001",
        "title": "Guard divide() against division-by-zero",
        "description": (
            "In calclib/calc.py, add a guard to the divide() function so that it "
            "raises ValueError when the divisor is zero. Add a corresponding pytest "
            "test in tests/test_calc.py."
        ),
        "type": "bugfix",
        "assigned_agent": "dev",
        "dependencies": [],
        "acceptance_criteria": ["divide(1, 0) raises ValueError"],
        "estimated_size": "small",
        "can_parallelize": True,
    },
    {
        "task_id": "T002",
        "title": "Add power(base, exp) function",
        "description": (
            "Add a power(base, exp) function to calclib/calc.py that returns base**exp. "
            "Add pytest tests in tests/test_calc.py covering positive, negative, and zero exponents."
        ),
        "type": "feature",
        "assigned_agent": "dev",
        "dependencies": [],
        "acceptance_criteria": ["power(2, 3) == 8", "power(2, 0) == 1"],
        "estimated_size": "small",
        "can_parallelize": True,
    },
]


def _load_tasks_from_architect(arch_path: str):
    data = json.loads(Path(arch_path).read_text())
    # Support both direct task list and context-file indirection
    context_file = data.get("_context_file")
    if context_file and Path(context_file).exists():
        data = json.loads(Path(context_file).read_text())
        data.pop("_meta", None)
    return data.get("tasks", [])


async def main():
    model    = os.getenv("LLM_MODEL")
    provider = os.getenv("LLM_PROVIDER")
    arch_path = os.getenv("ARCHITECT_OUTPUT")

    if arch_path:
        print(f"Using architect output from: {arch_path}")
        tasks = _load_tasks_from_architect(arch_path)
    else:
        print("Using built-in fallback task list")
        tasks = _FALLBACK_TASKS

    workflow_id = f"debug-decomposer-{int(time.time())}"

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"Decomposer isolation test")
    print(f"  model      : {model}")
    print(f"  provider   : {provider}")
    print(f"  project    : {PROJECT_NAME}  ({GITHUB_URL})")
    print(f"  tasks in   : {len(tasks)}")
    print(f"  workflow   : {workflow_id}")
    print(sep)

    results = []
    for task in tasks:
        task_copy = dict(task)
        task_copy["_workflow_id"] = workflow_id
        task_copy.setdefault("project_name", PROJECT_NAME)
        task_copy.setdefault("github_url", GITHUB_URL)

        t0 = time.time()
        result = await decomposer_activity(task_copy)
        elapsed = time.time() - t0

        # Load from context file if slim envelope
        context_file = result.get("_context_file")
        if context_file and Path(context_file).exists():
            full = json.loads(Path(context_file).read_text())
            full.pop("_meta", None)
        else:
            full = result

        subtasks = full.get("tasks", [full])
        print(f"\n  [{task.get('task_id', '?')}] {task.get('title', '')[:60]}")
        print(f"       → {len(subtasks)} task(s) in {elapsed:.1f}s  status={full.get('status', '?')}")
        for st in subtasks:
            tid   = st.get("task_id", "?")
            ttype = st.get("type", "?")
            agent = st.get("assigned_agent", "?")
            title = (st.get("title") or st.get("description", ""))[:70]
            print(f"       • {tid} [{ttype}/{agent}] {title}")
        results.append({"input": task, "output": full})

    print(f"\n{sep}")

    # Sanity checks across all output tasks
    all_out_tasks = []
    for r in results:
        full = r["output"]
        subtasks = full.get("tasks", [full])
        all_out_tasks.extend(subtasks)

    missing_type  = [t.get("task_id") for t in all_out_tasks if not t.get("type")]
    missing_title = [t.get("task_id") for t in all_out_tasks if not t.get("title")]
    missing_agent = [t.get("task_id") for t in all_out_tasks if not t.get("assigned_agent")]

    rc = 0
    if missing_type:
        print(f"  ⚠  Missing 'type'          : {missing_type}")
        rc = 1
    if missing_title:
        print(f"  ⚠  Missing 'title'         : {missing_title}")
        rc = 1
    if missing_agent:
        print(f"  ⚠  Missing 'assigned_agent': {missing_agent}")
        rc = 1
    if not missing_type and not missing_title and not missing_agent:
        print("  ✓ All output tasks have type, title, and assigned_agent")

    out = Path(f"/tmp/debug_decomposer_{workflow_id}.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"Full JSON dump : {out}")
    print(sep)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
