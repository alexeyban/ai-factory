"""End-to-end test runner with a set of simple, fast tasks.

Launches an OrchestratorWorkflow with a self-contained project brief that
requires no external GitHub repo. All tasks are small Python utilities that
can be implemented and tested in a single dev→QA cycle.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/e2e_simple.py

Environment:
    TEMPORAL_ADDRESS  defaults to localhost:7233
    TASK_QUEUE        defaults to ai-factory-tasks
    MOCK_LLM          set to 'true' to skip real LLM calls
    E2E_TIMEOUT_MIN   per-workflow timeout in minutes (default: 45)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import timedelta

from temporalio.client import Client

from orchestrator.workflows import OrchestratorWorkflow


# ---------------------------------------------------------------------------
# Task suite definitions
# ---------------------------------------------------------------------------

E2E_TASKS = [
    {
        "name": "math-utils",
        "description": (
            "Create a small Python utility library `mathutils/math_utils.py` with the following functions:\n"
            "\n"
            "1. `is_prime(n: int) -> bool` — return True if n is a prime number\n"
            "2. `fibonacci(n: int) -> list[int]` — return first n Fibonacci numbers\n"
            "3. `gcd(a: int, b: int) -> int` — greatest common divisor using Euclidean algorithm\n"
            "4. `lcm(a: int, b: int) -> int` — least common multiple\n"
            "\n"
            "Write pytest tests in `tests/test_math_utils.py` covering edge cases.\n"
            "All functions must have type hints and docstrings.\n"
        ),
    },
    {
        "name": "string-tools",
        "description": (
            "Create a Python module `strtools/string_tools.py` with the following functions:\n"
            "\n"
            "1. `count_words(text: str) -> int` — count words in a string\n"
            "2. `reverse_words(text: str) -> str` — reverse word order in a sentence\n"
            "3. `is_palindrome(s: str) -> bool` — check if a string is a palindrome (ignore case and spaces)\n"
            "4. `truncate(text: str, max_len: int, suffix: str = '...') -> str` — truncate text to max_len chars\n"
            "\n"
            "Write pytest tests in `tests/test_string_tools.py` covering edge cases.\n"
            "All functions must have type hints and docstrings.\n"
        ),
    },
    {
        "name": "data-structures",
        "description": (
            "Implement a simple Stack data structure in `datastructs/stack.py`:\n"
            "\n"
            "class Stack:\n"
            "  - `push(item) -> None` — push item onto stack\n"
            "  - `pop() -> Any` — pop and return top item (raise IndexError if empty)\n"
            "  - `peek() -> Any` — return top item without removing (raise IndexError if empty)\n"
            "  - `is_empty() -> bool` — return True if stack is empty\n"
            "  - `size() -> int` — return number of items\n"
            "  - `__len__` and `__repr__` support\n"
            "\n"
            "Write pytest tests in `tests/test_stack.py` covering push/pop/peek/empty/size.\n"
            "Use type hints throughout.\n"
        ),
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_e2e(task_suite: list[dict]) -> dict:
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TASK_QUEUE", "ai-factory-tasks")
    timeout_min = int(os.getenv("E2E_TIMEOUT_MIN", "45"))

    print(f"Connecting to Temporal at {temporal_address}...")
    client = await Client.connect(temporal_address, namespace=temporal_namespace)
    print("Connected.\n")

    results = []
    for suite in task_suite:
        name = suite["name"]
        description = suite["description"]
        workflow_id = f"e2e-{name}-{int(time.time())}"

        # Minimal self-contained task: no external GitHub repo needed.
        # The PM will create a new local project.
        initial_task = {
            "task_id": workflow_id,
            "description": description,
            "project_name": f"e2e-{name}",
            "github_url": "",          # no remote repo — local only
        }

        print(f"{'=' * 60}")
        print(f"Task: {name}")
        print(f"Workflow ID: {workflow_id}")
        print(f"{'=' * 60}")

        start = time.time()
        try:
            handle = await client.start_workflow(
                OrchestratorWorkflow.run,
                initial_task,
                id=workflow_id,
                task_queue=task_queue,
                execution_timeout=timedelta(minutes=timeout_min),
            )
            print(f"  Started — monitor at http://localhost:8088/namespaces/{temporal_namespace}/workflows/{workflow_id}")

            result = await handle.result(rpc_timeout=timedelta(minutes=timeout_min))
            elapsed = time.time() - start
            status = result.get("status", "unknown") if isinstance(result, dict) else "completed"
            print(f"  DONE in {elapsed:.0f}s — status: {status}")
            results.append({"name": name, "workflow_id": workflow_id, "status": status, "elapsed_s": round(elapsed)})

        except Exception as exc:
            elapsed = time.time() - start
            print(f"  FAILED in {elapsed:.0f}s — {exc}")
            results.append({"name": name, "workflow_id": workflow_id, "status": "error", "error": str(exc), "elapsed_s": round(elapsed)})

        print()

    return {"results": results}


def print_summary(report: dict) -> None:
    results = report["results"]
    passed = [r for r in results if r["status"] in ("complete", "completed", "success")]
    needs_attention = [r for r in results if r["status"] == "needs_attention"]
    failed = [r for r in results if r["status"] in ("error", "failed")]

    print("=" * 60)
    print("E2E TEST SUMMARY")
    print("=" * 60)
    for r in results:
        icon = "✓" if r["status"] in ("complete", "completed", "success") else ("~" if r["status"] == "needs_attention" else "✗")
        print(f"  {icon} {r['name']:<25} {r['status']:<20} {r['elapsed_s']}s")
    print("-" * 60)
    print(f"  Passed: {len(passed)}  Needs attention: {len(needs_attention)}  Failed: {len(failed)}")
    print("=" * 60)

    # Write JSON report
    report_path = f"/tmp/e2e_report_{int(time.time())}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull report: {report_path}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    report = asyncio.run(run_e2e(E2E_TASKS))
    print_summary(report)
