"""End-to-end smoke test: launch a tiny workflow and wait for completion.

Usage:
    .venv/bin/python scripts/run_e2e_test.py

The workflow creates a minimal Python project (a simple calculator library
with unit tests) — expected to produce 2-4 tasks, exercise PM → architect →
decomposer → dev → QA → analyst, and finish in under 30 minutes.
"""
import asyncio
import json
import os
import time
from datetime import timedelta

from temporalio.client import Client

from orchestrator.workflows import OrchestratorWorkflow

DESCRIPTION = """Create a small Python utility library called `calclib` with the following:

1. A module `calclib/calc.py` with four functions:
   - `add(a, b)` — returns a + b
   - `subtract(a, b)` — returns a - b
   - `multiply(a, b)` — returns a * b
   - `divide(a, b)` — returns a / b, raises ValueError if b == 0

2. A test file `tests/test_calc.py` with pytest tests covering:
   - basic operations
   - division by zero raises ValueError

3. A `README.md` with one-paragraph description of the library.

Keep it simple and minimal. No external dependencies beyond pytest.
"""


async def main():
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(temporal_address)

    workflow_id = f"e2e-calclib-{int(time.time())}"
    initial_task = {
        "task_id": workflow_id,
        "description": DESCRIPTION,
        "project_name": "calclib",
        "_workflow_id": workflow_id,
    }

    print(f"\n{'='*60}")
    print(f"Starting e2e test workflow: {workflow_id}")
    print(f"Monitor: http://localhost:8080/namespaces/default/workflows/{workflow_id}")
    print(f"{'='*60}\n")

    handle = await client.start_workflow(
        OrchestratorWorkflow.run,
        initial_task,
        id=workflow_id,
        task_queue=os.getenv("TASK_QUEUE", "ai-factory-tasks"),
        execution_timeout=timedelta(hours=1),
    )

    print("Workflow started. Waiting for result (up to 60 min)...")
    start = time.time()

    try:
        result = await handle.result(rpc_timeout=timedelta(hours=1))
    except Exception as exc:
        print(f"\n[FAIL] Workflow raised: {exc}")
        return 1

    elapsed = int(time.time() - start)
    status = result.get("status", "unknown")
    dev_qa = result.get("dev_qa_results", [])
    analysis = result.get("analysis", {})

    print(f"\n{'='*60}")
    print(f"Workflow finished in {elapsed}s")
    print(f"Status     : {status}")
    print(f"Tasks run  : {len(dev_qa)}")

    ok = all_ok = True
    for r in dev_qa:
        task_status = r.get("status", "?")
        qa_status = r.get("qa_status", "-")
        err = r.get("error", "")
        icon = "✓" if task_status == "success" else "✗"
        print(f"  {icon} [{r.get('task_id','?')}] status={task_status} qa={qa_status}" + (f" err={err[:80]}" if err else ""))
        if task_status != "success":
            ok = False

    print(f"\nAnalyst stage : {analysis.get('status', '?')}")
    print(f"Overall       : {'PASS' if ok and status in ('complete', 'needs_attention') else 'NEEDS REVIEW'}")
    print(f"{'='*60}\n")

    # Dump compact summary to file for CI / inspection
    summary_path = f"/tmp/e2e_{workflow_id}.json"
    with open(summary_path, "w") as f:
        json.dump({
            "workflow_id": workflow_id,
            "status": status,
            "elapsed_s": elapsed,
            "tasks": [
                {"id": r.get("task_id"), "status": r.get("status"), "qa": r.get("qa_status"), "error": r.get("error")}
                for r in dev_qa
            ],
        }, f, indent=2)
    print(f"Summary written to {summary_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    exit(asyncio.run(main()))
