from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

AGENT_TOPICS = {
    'architect': 'architect.tasks',
    'dev': 'dev.tasks',
    'qa': 'qa.tasks',
    'analyst': 'analyst.events',
}
ALLOWED_TASK_TYPES = {'feature', 'bugfix', 'refactor'}
SUCCESS_STAGES = {'architect_done', 'dev_done', 'qa_done', 'analysis_done'}
FAILURE_DECISIONS = {'retry', 'fail'}


def state_directory(workspace_root: str | Path = '/workspace') -> Path:
    return Path(workspace_root) / '.standalone_dispatcher' / 'plans'


def ensure_state_directory(workspace_root: str | Path = '/workspace') -> Path:
    path = state_directory(workspace_root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def plan_state_path(plan_id: str, workspace_root: str | Path = '/workspace') -> Path:
    return ensure_state_directory(workspace_root) / f'{plan_id}.json'


def load_all_plan_states(workspace_root: str | Path = '/workspace') -> dict[str, dict[str, Any]]:
    plans: dict[str, dict[str, Any]] = {}
    for path in ensure_state_directory(workspace_root).glob('*.json'):
        try:
            plans[path.stem] = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
    return plans


def save_plan_state(plan_state: dict[str, Any], workspace_root: str | Path = '/workspace') -> None:
    path = plan_state_path(plan_state['plan_id'], workspace_root)
    path.write_text(json.dumps(plan_state, indent=2, ensure_ascii=True), encoding='utf-8')


def dispatch_topic(agent_name: str | None) -> str | None:
    if not agent_name:
        return None
    return AGENT_TOPICS.get(agent_name)


def schema_safe_task_type(raw_type: str | None) -> str | None:
    if raw_type in ALLOWED_TASK_TYPES:
        return raw_type
    return None


def parse_plan_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    raw_logs = event.get('logs')
    if not raw_logs:
        return None
    try:
        payload = json.loads(raw_logs)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    execution_plan = payload.get('execution_plan')
    if not isinstance(execution_plan, list):
        return None
    return payload


def create_plan_state(plan_id: str, payload: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    tasks = {}
    task_order: list[str] = []
    for raw_task in payload.get('execution_plan', []):
        if not isinstance(raw_task, dict):
            continue
        task_id = raw_task.get('task_id')
        if not task_id:
            continue
        task_copy = deepcopy(raw_task)
        task_copy.setdefault('dependencies', [])
        task_copy.setdefault('acceptance_criteria', [])
        tasks[task_id] = task_copy
        task_order.append(task_id)
    return {
        'plan_id': plan_id,
        'project_goal': payload.get('project_goal'),
        'delivery_summary': payload.get('delivery_summary'),
        'artifact': event.get('artifact'),
        'task_order': task_order,
        'tasks': tasks,
        'dispatched': [],
        'completed': [],
        'failed': [],
        'event_history': [
            {
                'event_id': event.get('event_id'),
                'stage': event.get('stage'),
                'task_id': event.get('task_id'),
            }
        ],
    }


def find_plan_id_by_task_id(task_id: str, plans: dict[str, dict[str, Any]]) -> str | None:
    for plan_id, plan_state in plans.items():
        if task_id in plan_state.get('tasks', {}):
            return plan_id
    return None


def ready_tasks(plan_state: dict[str, Any]) -> list[dict[str, Any]]:
    completed = set(plan_state.get('completed', []))
    dispatched = set(plan_state.get('dispatched', []))
    ready: list[dict[str, Any]] = []
    for task_id in plan_state.get('task_order', []):
        if task_id in completed or task_id in dispatched:
            continue
        task = plan_state['tasks'][task_id]
        if dispatch_topic(task.get('assigned_agent')) is None:
            continue
        dependencies = set(task.get('dependencies', []))
        if dependencies.issubset(completed):
            ready.append(task)
    return ready


def mark_dispatched(plan_state: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    dispatched = set(plan_state.get('dispatched', []))
    for task in tasks:
        task_id = task.get('task_id')
        if task_id:
            dispatched.add(task_id)
    plan_state['dispatched'] = sorted(dispatched)


def apply_completion_event(plan_state: dict[str, Any], event: dict[str, Any]) -> bool:
    task_id = event.get('task_id')
    if not task_id or task_id not in plan_state.get('tasks', {}):
        return False
    plan_state.setdefault('event_history', []).append(
        {
            'event_id': event.get('event_id'),
            'stage': event.get('stage'),
            'task_id': task_id,
            'decision': event.get('decision'),
        }
    )
    decision = event.get('decision')
    if event.get('stage') in SUCCESS_STAGES and decision in {None, 'continue', 'complete'}:
        completed = set(plan_state.get('completed', []))
        completed.add(task_id)
        plan_state['completed'] = sorted(completed)
        failed = set(plan_state.get('failed', []))
        failed.discard(task_id)
        plan_state['failed'] = sorted(failed)
        return True
    if decision in FAILURE_DECISIONS:
        failed = set(plan_state.get('failed', []))
        failed.add(task_id)
        plan_state['failed'] = sorted(failed)
    return False


def build_task_message(task: dict[str, Any], plan_state: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(task)
    payload['_root_plan_id'] = plan_state['plan_id']
    payload['_project_goal'] = plan_state.get('project_goal')
    return {
        'task_id': task['task_id'],
        'description': task.get('description', task.get('title', '')),
        'type': schema_safe_task_type(task.get('type')),
        'dependencies': task.get('dependencies', []),
        'acceptance_criteria': task.get('acceptance_criteria', []),
        'artifact': plan_state.get('artifact'),
        'status': 'planned',
        'logs': json.dumps(payload, ensure_ascii=True),
    }


def process_event(event: dict[str, Any], workspace_root: str | Path = '/workspace') -> list[dict[str, Any]]:
    plans = load_all_plan_states(workspace_root)
    stage = event.get('stage')
    if stage == 'pm_done':
        payload = parse_plan_payload(event)
        if payload is None:
            return []
        plan_id = event.get('task_id') or event.get('event_id')
        if not plan_id:
            return []
        plan_state = create_plan_state(plan_id, payload, event)
        tasks = ready_tasks(plan_state)
        mark_dispatched(plan_state, tasks)
        save_plan_state(plan_state, workspace_root)
        return tasks

    task_id = event.get('task_id')
    if not task_id:
        return []
    plan_id = find_plan_id_by_task_id(task_id, plans)
    if plan_id is None:
        return []
    plan_state = plans[plan_id]
    progress_made = apply_completion_event(plan_state, event)
    tasks = ready_tasks(plan_state) if progress_made else []
    mark_dispatched(plan_state, tasks)
    save_plan_state(plan_state, workspace_root)
    return tasks
