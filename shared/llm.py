import json
import os
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from openai import OpenAI


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() == "true"


MOCK_MODE = _env_flag("MOCK_LLM")


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: Optional[str]
    base_url: Optional[str]
    temperature: float
    max_tokens: Optional[int]
    timeout: float


def _normalize_provider(value: Optional[str]) -> str:
    if not value:
        return "openai"
    return value.strip().lower()


def _normalize_model_name(provider: str, model: str) -> str:
    normalized = model.strip()

    if provider == "opencode":
        if normalized == "opencode/bigpickle":
            return "big-pickle"
        if normalized == "bigpickle":
            return "big-pickle"

    if "/" in normalized:
        model_provider, model_name = normalized.split("/", 1)
        if model_provider.strip().lower() == provider:
            return model_name.strip()

    return normalized


def _infer_provider_from_model(model: Optional[str], provider: Optional[str]) -> str:
    if provider:
        return _normalize_provider(provider)

    if model and "/" in model:
        return _normalize_provider(model.split("/", 1)[0])

    return _normalize_provider(os.getenv("LLM_PROVIDER", "openai"))


def _default_base_url(provider: str) -> Optional[str]:
    defaults = {
        "openai": os.getenv("OPENAI_BASE_URL"),
        "opencode": "https://opencode.ai/zen/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    }
    return defaults.get(provider, os.getenv("LLM_BASE_URL"))


def _default_api_key(provider: str) -> Optional[str]:
    env_by_provider = {
        "openai": "OPENAI_API_KEY",
        "opencode": "OPENCODE_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "ollama": "OLLAMA_API_KEY",
    }
    env_name = env_by_provider.get(provider, "LLM_API_KEY")
    return os.getenv(env_name) or os.getenv("LLM_API_KEY")


def load_llm_config(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> LLMConfig:
    resolved_provider = _infer_provider_from_model(model, provider)
    configured_model = model or os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1"
    resolved_model = _normalize_model_name(resolved_provider, configured_model)

    return LLMConfig(
        provider=resolved_provider,
        model=resolved_model,
        api_key=api_key or _default_api_key(resolved_provider),
        base_url=base_url or os.getenv("LLM_BASE_URL") or _default_base_url(resolved_provider),
        temperature=temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.2")),
        max_tokens=max_tokens if max_tokens is not None else _optional_int(os.getenv("LLM_MAX_TOKENS")),
        timeout=timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT", "120")),
    )


def _optional_int(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _build_messages(system_prompt: str, user_prompt: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    return messages


def _create_client(config: LLMConfig) -> OpenAI:
    client_kwargs = {
        "api_key": config.api_key or "not-needed",
        "timeout": config.timeout,
    }
    if config.base_url:
        client_kwargs["base_url"] = config.base_url
    return OpenAI(**client_kwargs)


def _extract_text_from_completion(completion) -> str:
    if getattr(completion, "choices", None):
        message = completion.choices[0].message
        if message.content:
            return message.content
    return ""


def _mock_architect_response(user_prompt: str) -> str:
    tasks = [
        {"task_id": str(uuid.uuid4()), "description": "Create FastAPI app structure"},
        {
            "task_id": str(uuid.uuid4()),
            "description": "Implement todo model and routes",
        },
        {
            "task_id": str(uuid.uuid4()),
            "description": "Add unit tests for API endpoints",
        },
    ]
    return json.dumps(tasks)


def _mock_pm_response(user_prompt: str) -> str:
    return json.dumps(
        {
            "project_goal": "Build REST API for todo app",
            "delivery_summary": "Deliver a todo API through coordinated architecture, implementation, QA, and project-state tracking.",
            "architect_guidance": [
                "Use FastAPI for the service layer.",
                "Keep task boundaries small enough for parallel development.",
                "Define the API contract before implementation begins.",
            ],
            "analyst_guidance": [
                "Track CRUD coverage and user-facing scope.",
                "Capture assumptions, open risks, and completion criteria.",
                "Report final status in a project state document.",
            ],
            "execution_plan": [
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Define API structure",
                    "description": "Architect the todo API modules, endpoints, and data flow.",
                    "assigned_agent": "architect",
                    "dependencies": [],
                    "acceptance_criteria": [
                        "Core endpoints are identified",
                        "Implementation slices are ready for dev handoff",
                    ],
                },
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Implement FastAPI application",
                    "description": "Create the FastAPI app structure and todo CRUD routes.",
                    "assigned_agent": "dev",
                    "dependencies": [],
                    "acceptance_criteria": [
                        "Application starts successfully",
                        "CRUD routes are implemented",
                    ],
                },
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Validate generated implementation",
                    "description": "Run tests against the generated workspace artifacts.",
                    "assigned_agent": "qa",
                    "dependencies": ["Implement FastAPI application"],
                    "acceptance_criteria": [
                        "Tests execute without infrastructure errors",
                        "Failures are captured in logs",
                    ],
                },
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Publish delivery status",
                    "description": "Update project state and summarize progress, risks, and completion.",
                    "assigned_agent": "analyst",
                    "dependencies": ["Validate generated implementation"],
                    "acceptance_criteria": [
                        "State file reflects latest task outcomes",
                        "Project status is explicit",
                    ],
                },
            ],
        }
    )


def _mock_dev_response(user_prompt: str) -> str:
    return '''"""Generated FastAPI application"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="Todo API")

class Todo(BaseModel):
    id: Optional[int] = None
    title: str
    completed: bool = False

todos: List[Todo] = []

@app.get("/todos", response_model=List[Todo])
async def get_todos():
    return todos

@app.post("/todos", response_model=Todo)
async def create_todo(todo: Todo):
    todo.id = len(todos) + 1
    todos.append(todo)
    return todo

@app.put("/todos/{todo_id}", response_model=Todo)
async def update_todo(todo_id: int, todo: Todo):
    for t in todos:
        if t.id == todo_id:
            t.title = todo.title
            t.completed = todo.completed
            return t
    raise HTTPException(status_code=404, detail="Todo not found")

@app.delete("/todos/{todo_id}")
async def delete_todo(todo_id: int):
    for i, t in enumerate(todos):
        if t.id == todo_id:
            todos.pop(i)
            return {"message": "Deleted"}
    raise HTTPException(status_code=404, detail="Todo not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''


def _mock_qa_response(user_prompt: str) -> str:
    return json.dumps(
        {
            "status": "success",
            "failing_tests": [],
            "error_summary": "",
            "root_cause": "",
            "fix_suggestion": "No action required.",
        }
    )


def _mock_analyst_response(user_prompt: str) -> str:
    return """# Project State

## Completed Tasks
- Created FastAPI application with todo CRUD operations
- Implemented 3 API endpoints (GET, POST, PUT, DELETE)
- All tests passing

## Files Generated
- `/workspace/{uuid}.py` - Main application file

## Status: Complete
"""


def _mock_llm(system_prompt: str, user_prompt: str) -> str:
    system_lower = system_prompt.lower()

    if "architect" in system_lower:
        return _mock_architect_response(user_prompt)
    if "project manager" in system_lower or "senior pm" in system_lower:
        return _mock_pm_response(user_prompt)
    if "qa" in system_lower:
        return _mock_qa_response(user_prompt)
    if "analyst" in system_lower:
        return _mock_analyst_response(user_prompt)
    if "dev" in system_lower or "senior" in system_lower:
        return _mock_dev_response(user_prompt)
    return json.dumps([{"task_id": str(uuid.uuid4()), "description": user_prompt}])


def call_llm(
    system_prompt: str,
    user_prompt: str = "",
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> str:
    if MOCK_MODE:
        return _mock_llm(system_prompt, user_prompt)

    config = load_llm_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    client = _create_client(config)

    request_kwargs = {
        "model": config.model,
        "messages": _build_messages(system_prompt, user_prompt),
        "temperature": config.temperature,
    }
    if config.max_tokens is not None:
        request_kwargs["max_tokens"] = config.max_tokens

    completion = client.chat.completions.create(**request_kwargs)
    return _extract_text_from_completion(completion)


def call_llm_with_messages(
    messages: Iterable[dict[str, str]],
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> str:
    if MOCK_MODE:
        user_prompt = "\n".join(message.get("content", "") for message in messages)
        return _mock_llm("", user_prompt)

    config = load_llm_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    client = _create_client(config)

    request_kwargs = {
        "model": config.model,
        "messages": list(messages),
        "temperature": config.temperature,
    }
    if config.max_tokens is not None:
        request_kwargs["max_tokens"] = config.max_tokens

    completion = client.chat.completions.create(**request_kwargs)
    return _extract_text_from_completion(completion)
