import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from shared.tracing import get_tracer

from openai import (
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
    OpenAI,
    RateLimitError,
)


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() == "true"


MOCK_MODE = _env_flag("MOCK_LLM")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: Optional[str]
    base_url: Optional[str]
    temperature: float
    max_tokens: Optional[int]
    timeout: float
    max_prompt_tokens: Optional[int]


FALLBACK_STATUS_CODES = {400, 401, 408, 409, 413, 429, 500, 502, 503, 504}
RATE_LIMIT_WINDOWS = {
    "gemini": {"max_requests": 10, "window_seconds": 60},
    "opencode": {"max_requests": 60, "window_seconds": 60},
}
PROVIDER_COOLDOWN_SECONDS = int(os.getenv("LLM_PROVIDER_COOLDOWN_SECONDS", str(15)))
_provider_request_times: dict[str, deque[float]] = {
    provider: deque() for provider in RATE_LIMIT_WINDOWS
}
_provider_cooldown_file = Path(
    os.getenv(
        "LLM_PROVIDER_COOLDOWN_FILE", "/tmp/ai_factory_llm_provider_cooldowns.json"
    )
)


def _normalize_provider(value: Optional[str]) -> str:
    if not value:
        return "openai"
    return value.strip().lower()


def _normalize_model_name(provider: str, model: str) -> str:
    normalized = model.strip()
    normalized_lower = normalized.lower()

    if provider == "opencode":
        if normalized == "opencode/bigpickle":
            return "big-pickle"
        if normalized == "bigpickle":
            return "big-pickle"
        if normalized_lower in {
            "big-pickle",
            "opencode big pickle",
            "opencode/big-pickle",
        }:
            return "big-pickle"
        if normalized_lower in {
            "minimax/minimax-m2.5-free",
            "minimax/minimax-m2.5",
            "minimax m2.5 free",
            "minimax m2.5free",
            "m2.5-free",
            "m2.5 free",
        }:
            # Correct API name — minimax/MiniMax-M2.5-Free returns 401
            return "minimax-m2.5-free"

    if provider == "openai":
        if normalized_lower in {
            "gpt-5.4 mini",
            "gpt5.4mini",
            "gpt-5-mini",
            "gpt 5 mini",
        }:
            return "gpt-5-mini"

    if provider == "gemini":
        if normalized_lower in {"gemini 3 flash", "gemini-3-flash", "gemini3flash"}:
            return "gemini-2.5-flash"

    if provider == "ollama":
        ollama_aliases = {
            "llama 4": "llama4:scout",
            "llama4": "llama4:scout",
            "llama-4": "llama4:scout",
            "deepseek v4": "deepseek-v3",
            "deepseek-v4": "deepseek-v3",
            "deepseekv4": "deepseek-v3",
            "qwen 3.5": "qwen3.5:35b-a3b",
            "qwen3.5": "qwen3.5:35b-a3b",
            "qwen-3.5": "qwen3.5:35b-a3b",
        }
        if normalized_lower in ollama_aliases:
            return ollama_aliases[normalized_lower]

    if provider == "deepseek":
        if normalized_lower in {"deepseek v4", "deepseek-v4", "deepseekv4"}:
            return "deepseek-chat"

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
        "deepseek": "https://api.deepseek.com",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "ollama": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    }
    return defaults.get(provider, os.getenv("LLM_BASE_URL"))


def _default_api_key(provider: str) -> Optional[str]:
    env_by_provider = {
        "openai": "OPENAI_API_KEY",
        "opencode": "OPENCODE_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "ollama": "OLLAMA_API_KEY",
    }
    env_name = env_by_provider.get(provider, "LLM_API_KEY")
    if provider == "ollama":
        return (
            os.getenv("OLLAMA_API_KEY")
            or os.getenv("LLM_API_KEY")
        )
    return os.getenv(env_name) or os.getenv("LLM_API_KEY")


def _default_model_for_provider(provider: str) -> str:
    defaults = {
        "openai": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "opencode": os.getenv("OPENCODE_MODEL", "glm-5"),
        "openrouter": os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
        "deepseek": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "gemini": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "ollama": os.getenv("OLLAMA_MODEL") or "llama4:scout",
        "claude": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
    }
    return defaults.get(provider, os.getenv("LLM_MODEL", "gpt-4.1"))


def _explicit_fallback_chain() -> list[str]:
    raw = os.getenv("LLM_FALLBACK_ORDER", "")
    if not raw.strip():
        return []
    return [_normalize_provider(item) for item in raw.split(",") if item.strip()]


def _provider_has_credentials(provider: str) -> bool:
    if provider == "ollama":
        return bool(
            os.getenv("OLLAMA_BASE_URL")
            or os.getenv("OLLAMA_MODEL")
            or os.getenv("OLLANA_API_KEY")
            or os.getenv("OLLAMA_API_KEY")
        )
    if provider == "claude":
        import shutil
        return bool(shutil.which("claude") or os.getenv("ANTHROPIC_API_KEY"))
    return bool(_default_api_key(provider))


def _build_fallback_chain(primary_provider: str) -> list[str]:
    configured = _explicit_fallback_chain()
    if configured:
        ordered = [primary_provider, *configured]
    else:
        ordered = [primary_provider, "gemini", "openai", "deepseek", "ollama"]

    deduped: list[str] = []
    for provider in ordered:
        normalized = _normalize_provider(provider)
        if normalized in deduped:
            continue
        if normalized != primary_provider and not _provider_has_credentials(normalized):
            continue
        deduped.append(normalized)
    return deduped


def _config_for_provider(
    provider: str,
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    timeout: Optional[float],
) -> LLMConfig:
    requested_model = model
    if requested_model and "/" in requested_model:
        requested_provider = _normalize_provider(requested_model.split("/", 1)[0])
        if requested_provider != provider:
            requested_model = None

    return load_llm_config(
        model=requested_model or _default_model_for_provider(provider),
        provider=provider,
        api_key=api_key
        if provider == _infer_provider_from_model(model, provider)
        else None,
        base_url=base_url
        if provider == _infer_provider_from_model(model, provider)
        else None,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def _is_retryable_llm_error(exc: Exception) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in FALLBACK_STATUS_CODES
    return False


def _load_provider_cooldowns() -> dict[str, float]:
    if not _provider_cooldown_file.exists():
        return {}
    try:
        raw = json.loads(_provider_cooldown_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    return {
        str(provider): float(until_ts)
        for provider, until_ts in raw.items()
        if isinstance(until_ts, (int, float)) and float(until_ts) > now
    }


def _save_provider_cooldowns(cooldowns: dict[str, float]) -> None:
    _provider_cooldown_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        provider: until_ts
        for provider, until_ts in cooldowns.items()
        if until_ts > time.time()
    }
    _provider_cooldown_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    )


def _provider_cooldown_remaining(provider: str) -> float:
    cooldowns = _load_provider_cooldowns()
    return max(0.0, cooldowns.get(provider, 0.0) - time.time())


def _mark_provider_rate_limited(
    provider: str, cooldown_seconds: int = PROVIDER_COOLDOWN_SECONDS
) -> None:
    cooldowns = _load_provider_cooldowns()
    cooldowns[provider] = time.time() + cooldown_seconds
    _save_provider_cooldowns(cooldowns)
    LOGGER.warning(
        "Provider %s entered cooldown for %s seconds after rate limit",
        provider,
        cooldown_seconds,
    )


def _clear_provider_cooldown(provider: str) -> None:
    cooldowns = _load_provider_cooldowns()
    if provider in cooldowns:
        cooldowns.pop(provider, None)
        _save_provider_cooldowns(cooldowns)
        LOGGER.info("Provider %s cooldown cleared after successful response", provider)


def _reset_all_cooldowns() -> None:
    """Reset all provider cooldowns"""
    cooldowns = _load_provider_cooldowns()
    cooldowns.clear()
    _save_provider_cooldowns(cooldowns)
    LOGGER.info("All provider cooldowns reset")


def _is_provider_on_cooldown(provider: str) -> bool:
    if os.getenv("LLM_RESET_COOLDOWNS", "false").lower() == "true":
        _reset_all_cooldowns()
        os.environ.pop("LLM_RESET_COOLDOWNS", None)
        return False
    return _provider_cooldown_remaining(provider) > 0


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    return isinstance(exc, APIStatusError) and exc.status_code == 429


def _call_claude_subprocess(
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: float = 120.0,
) -> str:
    """Invoke claude CLI as subprocess — uses ~/.claude/ subscription credentials or ANTHROPIC_API_KEY."""
    import subprocess

    # Note: do NOT use --bare; it disables OAuth/subscription auth (only ANTHROPIC_API_KEY works with --bare).
    # --no-session-persistence avoids polluting the session history.
    cmd = [
        "claude",
        "-p", user_prompt,
        "--output-format", "json",
        "--no-session-persistence",
    ]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}"
        )

    data = json.loads(proc.stdout)
    return data.get("result") or data.get("text") or str(data)


def _request_with_fallback(
    *,
    messages: list[dict[str, str]],
    model: Optional[str],
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    timeout: Optional[float],
    max_prompt_tokens: Optional[int],
) -> str:
    primary_provider = _infer_provider_from_model(model, provider)
    fallback_chain = _build_fallback_chain(primary_provider)
    last_error: Exception | None = None

    for fallback_provider in fallback_chain:
        if _is_provider_on_cooldown(fallback_provider):
            LOGGER.info(
                "Skipping provider %s because cooldown is active for another %.0f seconds",
                fallback_provider,
                _provider_cooldown_remaining(fallback_provider),
            )
            continue
        if fallback_provider == "claude":
            try:
                system_msg = next(
                    (m["content"] for m in messages if m["role"] == "system"), ""
                )
                user_msg = next(
                    (m["content"] for m in messages if m["role"] == "user"), ""
                )
                result = _call_claude_subprocess(
                    system_msg,
                    user_msg,
                    _default_model_for_provider("claude"),
                    timeout=timeout or 120.0,
                )
                _record_provider_request("claude")
                return result
            except Exception as exc:
                LOGGER.warning("claude subprocess failed: %s", exc)
                last_error = exc
                continue

        config = _config_for_provider(
            fallback_provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        client = _create_client(config)
        _wait_for_rate_limit_slot(fallback_provider)
        request_kwargs = {
            "model": config.model,
            "messages": messages,
        }
        if _supports_custom_temperature(config.provider, config.model):
            request_kwargs["temperature"] = config.temperature
        if config.max_tokens is not None:
            request_kwargs["max_tokens"] = config.max_tokens

        _tracer = get_tracer("shared.llm")
        with _tracer.start_as_current_span("llm.chat_completion") as span:
            span.set_attribute("llm.provider", fallback_provider)
            span.set_attribute("llm.model", config.model)
            span.set_attribute(
                "llm.prompt_tokens",
                sum(len(m.get("content", "")) // 4 for m in messages),
            )
            try:
                completion = client.chat.completions.create(**request_kwargs)
                _record_provider_request(fallback_provider)
                _clear_provider_cooldown(fallback_provider)
                result = _extract_text_from_completion(completion)
                span.set_attribute("llm.response_tokens", len(result) // 4)
                return result
            except Exception as exc:
                _record_provider_request(fallback_provider)
                span.record_exception(exc)
                if _is_rate_limit_error(exc):
                    _mark_provider_rate_limited(fallback_provider)
                last_error = exc
                if not _is_retryable_llm_error(exc):
                    raise
                continue

    if last_error:
        raise last_error
    raise RuntimeError(
        "No LLM providers were available for the request; all candidates may be cooling down or unavailable"
    )


def _supports_custom_temperature(provider: str, model: str) -> bool:
    model_name = model.lower()
    if provider == "openai" and model_name.startswith(("gpt-5", "o1", "o3", "o4")):
        return False
    return True


def _wait_for_rate_limit_slot(provider: str) -> None:
    limits = RATE_LIMIT_WINDOWS.get(provider)
    if not limits:
        return

    request_times = _provider_request_times.setdefault(provider, deque())
    now = time.monotonic()
    window_seconds = limits["window_seconds"]
    while request_times and now - request_times[0] >= window_seconds:
        request_times.popleft()

    if len(request_times) < limits["max_requests"]:
        return

    wait_seconds = window_seconds - (now - request_times[0])
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    now = time.monotonic()
    while request_times and now - request_times[0] >= window_seconds:
        request_times.popleft()


def _record_provider_request(provider: str) -> None:
    if provider not in RATE_LIMIT_WINDOWS:
        return
    request_times = _provider_request_times.setdefault(provider, deque())
    now = time.monotonic()
    request_times.append(now)


def load_llm_config(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    max_prompt_tokens: Optional[int] = None,
) -> LLMConfig:
    resolved_provider = _infer_provider_from_model(model, provider)
    configured_model = (
        model
        or os.getenv("LLM_MODEL")
        or _default_model_for_provider(resolved_provider)
    )
    resolved_model = _normalize_model_name(resolved_provider, configured_model)

    return LLMConfig(
        provider=resolved_provider,
        model=resolved_model,
        api_key=api_key or _default_api_key(resolved_provider),
        base_url=base_url
        or os.getenv("LLM_BASE_URL")
        or _default_base_url(resolved_provider),
        temperature=temperature
        if temperature is not None
        else float(os.getenv("LLM_TEMPERATURE", "0.2")),
        max_tokens=max_tokens
        if max_tokens is not None
        else _optional_int(os.getenv("LLM_MAX_TOKENS")),
        timeout=timeout
        if timeout is not None
        else float(os.getenv("LLM_TIMEOUT", "300")),
        max_prompt_tokens=max_prompt_tokens
        if max_prompt_tokens is not None
        else _optional_int(os.getenv("LLM_MAX_PROMPT_TOKENS")),
    )


def _optional_int(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _build_messages(
    system_prompt: str,
    user_prompt: str,
    max_prompt_tokens: Optional[int] = None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})

    if max_prompt_tokens is not None and max_prompt_tokens > 0:
        # Approximate tokens by character count (rough heuristic: 1 token ~ 4 chars)
        max_chars = max_prompt_tokens * 4
        current_char_count = sum(len(msg.get("content", "")) for msg in messages)

        if current_char_count > max_chars:
            LOGGER.warning(
                "Prompt exceeds max_prompt_tokens (%s chars vs %s max). Truncating user message.",
                current_char_count,
                max_chars,
            )
            # Prioritize truncating the user message
            user_message_index = -1
            for i, msg in enumerate(messages):
                if msg.get("role") == "user":
                    user_message_index = i
                    break

            if user_message_index != -1:
                system_content_len = sum(
                    len(msg.get("content", ""))
                    for i, msg in enumerate(messages)
                    if i != user_message_index
                )
                remaining_chars_for_user = max(0, max_chars - system_content_len)
                original_user_content = messages[user_message_index].get("content", "")
                if len(original_user_content) > remaining_chars_for_user:
                    messages[user_message_index]["content"] = (
                        original_user_content[:remaining_chars_for_user]
                        + "\n\n... (truncated)"
                    )
            else:
                # If no user message, truncate the last message (system message or other)
                last_message = messages[-1]
                if len(last_message.get("content", "")) > max_chars:
                    last_message["content"] = (
                        last_message["content"][:max_chars] + "\n\n... (truncated)"
                    )

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

    # PM check must come before architect: PM system prompt now mentions "architect_guidance"
    if "project manager" in system_lower or "senior pm" in system_lower:
        return _mock_pm_response(user_prompt)
    if "solution architect" in system_lower or ("architect" in system_lower and "project manager" not in system_lower):
        return _mock_architect_response(user_prompt)
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
    max_prompt_tokens: Optional[int] = None,
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
        max_prompt_tokens=max_prompt_tokens,
    )
    return _request_with_fallback(
        messages=_build_messages(
            system_prompt, user_prompt, max_prompt_tokens=config.max_prompt_tokens
        ),
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature if temperature is not None else config.temperature,
        max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
        timeout=timeout if timeout is not None else config.timeout,
        max_prompt_tokens=max_prompt_tokens
        if max_prompt_tokens is not None
        else config.max_prompt_tokens,
    )


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
    max_prompt_tokens: Optional[int] = None,
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
        max_prompt_tokens=max_prompt_tokens,
    )
    return _request_with_fallback(
        messages=list(messages),
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature if temperature is not None else config.temperature,
        max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
        timeout=timeout if timeout is not None else config.timeout,
        max_prompt_tokens=max_prompt_tokens
        if max_prompt_tokens is not None
        else config.max_prompt_tokens,
    )
