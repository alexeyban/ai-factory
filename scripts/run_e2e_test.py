"""End-to-end smoke test: launch a tiny workflow against a real GitHub repo.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/run_e2e_test.py

The workflow targets https://github.com/alexeyban/calclib — a minimal Python
calculator library. Expected: PM → architect → decomposer → dev → QA →
analyst, producing a small number of tasks.

Exit codes:
    0 — all tasks succeeded
    1 — one or more tasks failed, or workflow itself failed
"""
import asyncio
import json
import os
import time
from datetime import timedelta

from temporalio.client import Client

from orchestrator.workflows import OrchestratorWorkflow

GITHUB_URL = "https://github.com/alexeyban/calclib"

DESCRIPTION = """Improve the calclib Python library at https://github.com/alexeyban/calclib.

calclib is a minimal Python calculator library with:
- calclib/calc.py: add, subtract, multiply, divide functions
- tests/test_calc.py: pytest tests

Tasks:
1. Ensure divide raises ValueError on division by zero (add the guard if missing)
2. Add a `power(base, exp)` function to calclib/calc.py and corresponding tests
3. Ensure README.md exists with a short description of the library

All changes must go into the https://github.com/alexeyban/calclib repository.
Clone it first, then make changes on a task branch and merge to main.
"""


async def main():
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(temporal_address)

    workflow_id = f"e2e-calclib-{int(time.time())}"
    initial_task = {
        "task_id": workflow_id,
        "description": DESCRIPTION,
        "project_name": "calclib",
        "github_url": GITHUB_URL,
        "_workflow_id": workflow_id,
    }

    print(f"\n{'='*60}")
    print(f"Starting e2e test workflow: {workflow_id}")
    print(f"GitHub repo : {GITHUB_URL}")
    print(f"Monitor     : http://localhost:8080/namespaces/default/workflows/{workflow_id}")
    print(f"{'='*60}\n")

    handle = await client.start_workflow(
        OrchestratorWorkflow.run,
        initial_task,
        id=workflow_id,
        task_queue=os.getenv("TASK_QUEUE", "ai-factory-tasks"),
        execution_timeout=timedelta(hours=1),
    )

    print("Workflow started — waiting for result (up to 60 min)...")
    start = time.time()

    try:
        result = await handle.result(rpc_timeout=timedelta(hours=1))
    except Exception as exc:
        print(f"\n[FAIL] Workflow raised: {exc}")
        return 1

    elapsed = int(time.time() - start)
    wf_status = result.get("status", "unknown")
    dev_qa = result.get("dev_qa_results", [])
    analysis = result.get("analysis", {})

    print(f"\n{'='*60}")
    print(f"Workflow finished in {elapsed}s  |  status={wf_status}")
    print(f"Tasks run: {len(dev_qa)}")
    print()

    any_fail = False
    for r in dev_qa:
        task_status = r.get("status", "?")
        qa_status   = r.get("qa_status", "-")
        err         = r.get("error") or ""
        if task_status == "fail" or (task_status != "success"):
            icon = "FAIL"
            any_fail = True
        else:
            icon = "PASS"
        line = f"  [{icon}] {r.get('task_id','?')} status={task_status} qa={qa_status}"
        if err:
            line += f"  error: {err[:100]}"
        print(line)

    print()
    print(f"Analyst: {analysis.get('status', '?')}")
    overall = "FAIL" if any_fail or wf_status not in ("complete", "needs_attention") else "PASS"
    print(f"Overall: {overall}")
    print(f"{'='*60}\n")

    summary = {
        "workflow_id": workflow_id,
        "status": wf_status,
        "elapsed_s": elapsed,
        "overall": overall,
        "tasks": [
            {
                "id": r.get("task_id"),
                "status": r.get("status"),
                "qa_status": r.get("qa_status"),
                "error": r.get("error"),
            }
            for r in dev_qa
        ],
    }
    summary_path = f"/tmp/e2e_{workflow_id}.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary: {summary_path}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
