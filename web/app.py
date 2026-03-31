"""
AI Factory Web UI — start and monitor OrchestratorWorkflow runs.

Endpoints:
  GET  /            HTML form to start a new project workflow
  POST /start       Validate inputs, start workflow, redirect to Temporal UI
  GET  /workflows   JSON list of recent OrchestratorWorkflow runs
  GET  /health      Liveness check
"""
import asyncio
import os
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from temporalio.client import Client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.getenv("TASK_QUEUE", "ai-factory-tasks")
TEMPORAL_WEB_URL = os.getenv("TEMPORAL_WEB_URL", "http://localhost:8080")

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="AI Factory", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Temporal client (lazy singleton)
# ---------------------------------------------------------------------------

_client: Client | None = None


async def _get_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    return _client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """Convert project name to a URL/filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


async def _running_workflows(client: Client) -> list[dict]:
    """Return list of currently running OrchestratorWorkflow instances."""
    running = []
    async for wf in client.list_workflows('WorkflowType="OrchestratorWorkflow"'):
        status_str = str(wf.status)
        if status_str in ("WORKFLOW_EXECUTION_STATUS_RUNNING", "Running", "1"):
            running.append({"id": wf.id, "status": "running"})
    return running


async def _fetch_github_readme(github_url: str) -> str:
    """Try to fetch README.md from a GitHub repository URL."""
    # Convert https://github.com/owner/repo to raw README URL
    match = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", github_url)
    if not match:
        return ""
    slug = match.group(1)
    for branch in ("main", "master"):
        raw_url = f"https://raw.githubusercontent.com/{slug}/{branch}/README.md"
        try:
            async with httpx.AsyncClient(timeout=10) as hx:
                resp = await hx.get(raw_url)
                if resp.status_code == 200:
                    return resp.text[:12000]  # cap at 12k chars
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = "", warning: str = ""):
    client = await _get_client()
    running = await _running_workflows(client)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "running_workflows": running,
            "temporal_web_url": TEMPORAL_WEB_URL,
            "temporal_namespace": TEMPORAL_NAMESPACE,
            "error": error,
            "warning": warning,
        },
    )


@app.post("/start")
async def start_workflow(
    request: Request,
    project_name: str = Form(...),
    github_url: str = Form(...),
    description: str = Form(""),
    fetch_readme: bool = Form(False),
    force_start: bool = Form(False),
):
    project_name = project_name.strip()
    github_url = github_url.strip()
    description = description.strip()

    # Validate
    if not project_name:
        return RedirectResponse("/?error=Project+name+is+required", status_code=303)
    if not github_url.startswith("https://"):
        return RedirectResponse("/?error=GitHub+URL+must+start+with+https://", status_code=303)

    # Optionally fetch README as description
    if fetch_readme or not description:
        readme = await _fetch_github_readme(github_url)
        if readme:
            description = readme
        elif not description:
            return RedirectResponse(
                "/?error=No+description+provided+and+README+could+not+be+fetched",
                status_code=303,
            )

    client = await _get_client()

    # Guard: warn if another workflow is already running
    if not force_start:
        running = await _running_workflows(client)
        if running:
            ids = ", ".join(w["id"] for w in running)
            warning = (
                f"WARNING: {len(running)} workflow(s) already running: {ids}. "
                "LLM rate limit contention likely. "
                "Check 'Force start' to proceed anyway."
            )
            return templates.TemplateResponse(
                "index.html",
                {
                    "request": request,
                    "running_workflows": running,
                    "temporal_web_url": TEMPORAL_WEB_URL,
                    "temporal_namespace": TEMPORAL_NAMESPACE,
                    "error": "",
                    "warning": warning,
                    "prefill": {
                        "project_name": project_name,
                        "github_url": github_url,
                        "description": description,
                    },
                },
                status_code=200,
            )

    workflow_id = f"{_slugify(project_name)}-{int(time.time())}"

    initial_task = {
        "task_id": workflow_id,
        "description": description,
        "project_name": _slugify(project_name),
        "github_url": github_url,
    }

    from orchestrator.workflows import OrchestratorWorkflow

    handle = await client.start_workflow(
        OrchestratorWorkflow.run,
        initial_task,
        id=workflow_id,
        task_queue=TASK_QUEUE,
        execution_timeout=timedelta(hours=12),
    )

    monitor_url = (
        f"{TEMPORAL_WEB_URL}/namespaces/{TEMPORAL_NAMESPACE}/workflows/{handle.id}"
    )
    return RedirectResponse(monitor_url, status_code=303)


@app.get("/workflows")
async def list_workflows():
    """JSON list of recent OrchestratorWorkflow runs (running + last 10 completed)."""
    client = await _get_client()
    results = []
    count = 0
    async for wf in client.list_workflows('WorkflowType="OrchestratorWorkflow"'):
        results.append(
            {
                "id": wf.id,
                "status": str(wf.status),
                "start_time": wf.start_time.isoformat() if wf.start_time else None,
                "close_time": wf.close_time.isoformat() if wf.close_time else None,
                "url": f"{TEMPORAL_WEB_URL}/namespaces/{TEMPORAL_NAMESPACE}/workflows/{wf.id}",
            }
        )
        count += 1
        if count >= 20:
            break
    return JSONResponse({"workflows": results, "count": len(results)})
