# AI Factory Agents

## Principle

One agent = one responsibility.

When a new capability is distinct, prefer creating a new agent or skill instead of expanding an existing one.

## Rules

- Keep task boundaries atomic and verifiable.
- Use explicit input/output contracts for every task.
- Prefer many small tasks over a few large ones.
- Add dependencies only when one task truly blocks another.

## Current agents

| Agent | File | Responsibility |
|-------|------|----------------|
| PM | `agents/pm/` | Planning and task breakdown |
| Architect | `agents/architect/` | Solution architecture |
| Decomposer | `agents/decomposer/` | Split large work into atomic subtasks |
| Dev | `agents/dev/` | Implementation |
| QA | `agents/qa/` | Validation and test feedback |
| Analyst | `agents/analyst/` | Project state tracking |
| Dispatcher | `agents/dispatcher/` | Standalone Kafka task orchestration and dependency-based dispatch |

## Standard task contract

Every task should carry the same core structure:

```json
{
  "task_id": "T001",
  "title": "Short descriptive title",
  "description": "Implementation-ready description",
  "type": "feature|bugfix|refactor|setup|test",
  "dependencies": [],
  "input": {
    "files": [],
    "context": "required context"
  },
  "output": {
    "files": [],
    "artifacts": [],
    "expected_result": "concrete result"
  },
  "verification": {
    "method": "pytest|manual|review",
    "test_file": null,
    "criteria": []
  },
  "acceptance_criteria": [],
  "estimated_size": "small|medium|large",
  "can_parallelize": true
}
```

## Workflow

1. `pm_activity` creates the project plan.
2. `architect_activity` validates architecture and returns tasks.
3. `decomposer_activity` splits oversized tasks before execution.
4. `process_all_tasks` runs the prepared tasks through dev and QA.
5. `analyst_activity` records the final project state.

## Notes

- Large prompts should be decomposed before they reach execution.
- Task outputs must remain machine-readable JSON whenever possible.
- Keep docs and prompts aligned with the standard task schema.
