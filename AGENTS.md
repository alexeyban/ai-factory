# Agents

When extending this system, prefer creating a separate agent or skill instead of overloading an existing one.

## Rule

- If a new responsibility is distinct, create a new agent.
- If the logic is reusable and narrow, create a skill.
- Keep task boundaries atomic and verifiable.
- Use explicit input/output contracts for every task.

## Current agents

- `pm`: planning and task decomposition
- `architect`: solution design
- `decomposer`: splits large work into atomic tasks
- `dev`: implementation
- `qa`: validation
- `analyst`: project state tracking
