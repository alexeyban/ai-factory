import os
import json
import uuid
from openai import OpenAI

MOCK_MODE = os.getenv("MOCK_LLM", "false").lower() == "true"

if not MOCK_MODE and os.getenv("OPENAI_API_KEY"):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _mock_architect_response(user_prompt: str) -> str:
    """Mock response for architect - returns JSON task list"""
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


def _mock_dev_response(user_prompt: str) -> str:
    """Mock response for dev - returns Python code"""
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
    """Mock response for qa - returns pytest output"""
    return """============================= test session starts ==============================
collected 3 items

tests/test_api.py::test_get_todos PASSED                               [ 33%]
tests/test_api.py::test_create_todo PASSED                            [ 66%]
tests/test_api.py::test_update_todo PASSED                            [100%]

============================== 3 passed in 0.12s =============================="""


def _mock_analyst_response(user_prompt: str) -> str:
    """Mock response for analyst - returns project state"""
    return """# Project State

## Completed Tasks
- Created FastAPI application with todo CRUD operations
- Implemented 3 API endpoints (GET, POST, PUT, DELETE)
- All tests passing

## Files Generated
- `/workspace/{uuid}.py` - Main application file

## Status: Complete
"""


def call_llm(system_prompt: str, user_prompt: str = "") -> str:
    """Call LLM with fallback to mock when no API key"""
    if MOCK_MODE or not os.getenv("OPENAI_API_KEY"):
        return _mock_llm(system_prompt, user_prompt)

    resp = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


def _mock_llm(system_prompt: str, user_prompt: str) -> str:
    """Mock LLM that returns appropriate responses based on system prompt"""
    system_lower = system_prompt.lower()

    if "architect" in system_lower:
        return _mock_architect_response(user_prompt)
    elif "dev" in system_lower or "senior" in system_lower:
        return _mock_dev_response(user_prompt)
    elif "qa" in system_lower:
        return _mock_qa_response(user_prompt)
    elif "analyst" in system_lower:
        return _mock_analyst_response(user_prompt)
    else:
        return json.dumps([{"task_id": str(uuid.uuid4()), "description": user_prompt}])
