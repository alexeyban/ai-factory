"""Decomposer agent for breaking large tasks into atomic sub-tasks."""

from shared.prompts.loader import load_prompt

SYSTEM_PROMPT = load_prompt("decomposer", "system")
USER_PROMPT_TEMPLATE = load_prompt("decomposer", "user")
