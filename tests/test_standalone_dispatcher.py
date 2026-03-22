import json

from shared.standalone_dispatcher import build_task_message, process_event


def _pm_done_event(plan_id: str = 'plan-1'):
    return {
        'event_id': 'evt-pm',
        'task_id': plan_id,
        'stage': 'pm_done',
        'artifact': '/tmp/spec.md',
        'logs': json.dumps(
            {
                'project_goal': 'Build Reversi AlphaZero',
                'delivery_summary': 'Plan foundation work',
                'execution_plan': [
                    {
                        'task_id': 'T001',
                        'title': 'Architecture doc',
                        'description': 'Write architecture doc',
                        'assigned_agent': 'architect',
                        'dependencies': [],
                        'acceptance_criteria': ['Doc exists'],
                        'type': 'feature',
                    },
                    {
                        'task_id': 'T002',
                        'title': 'Repo skeleton',
                        'description': 'Create repo skeleton',
                        'assigned_agent': 'dev',
                        'dependencies': ['T001'],
                        'acceptance_criteria': ['Repo layout exists'],
                        'type': 'setup',
                    },
                ],
            }
        ),
    }


def test_pm_done_dispatches_dependency_free_tasks(tmp_path):
    tasks = process_event(_pm_done_event(), workspace_root=tmp_path)
    assert [task['task_id'] for task in tasks] == ['T001']


def test_completion_event_releases_dependents(tmp_path):
    process_event(_pm_done_event(), workspace_root=tmp_path)
    follow_up = process_event(
        {
            'event_id': 'evt-arch',
            'task_id': 'T001',
            'stage': 'architect_done',
            'decision': 'continue',
        },
        workspace_root=tmp_path,
    )
    assert [task['task_id'] for task in follow_up] == ['T002']


def test_failure_does_not_release_dependents(tmp_path):
    process_event(_pm_done_event(), workspace_root=tmp_path)
    follow_up = process_event(
        {
            'event_id': 'evt-arch-fail',
            'task_id': 'T001',
            'stage': 'architect_done',
            'decision': 'retry',
        },
        workspace_root=tmp_path,
    )
    assert follow_up == []


def test_task_message_uses_schema_safe_type():
    message = build_task_message(
        {
            'task_id': 'T002',
            'title': 'Repo skeleton',
            'description': 'Create repo skeleton',
            'dependencies': [],
            'acceptance_criteria': [],
            'type': 'setup',
        },
        {'plan_id': 'plan-1', 'artifact': '/tmp/spec.md', 'project_goal': 'Goal'},
    )
    assert message['type'] is None
    assert message['task_id'] == 'T002'
