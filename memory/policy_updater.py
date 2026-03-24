"""Policy Updater — adjusts Dev agent prompts and skill weights after each episode."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

_DEFAULT_POLICY_STATE_PATH = (
    Path(os.getenv("AI_FACTORY_WORKSPACE", "workspace"))
    / ".ai_factory"
    / "policy_state.json"
)
_DEFAULT_EXAMPLES_PATH = Path("shared/prompts/dev/examples.json")
_MAX_EXAMPLES = 3
_MIN_EXPLORATION_RATE = 0.1


class PolicyUpdater:
    """Updates the Dev agent policy based on episode results.

    Three mechanisms:
    1. _update_prompt_examples — saves best solutions as few-shot examples
       (reward > 0.8 → persisted to shared/prompts/dev/examples.json)
    2. _update_skill_weights   — nudges PostgreSQL success_rate for used skills
    3. _update_exploration_rate — adaptive epsilon decay when performance is good
    """

    def __init__(
        self,
        replay_buffer=None,
        db=None,
        skill_registry=None,
        policy_state_path: Path = _DEFAULT_POLICY_STATE_PATH,
        examples_path: Path = _DEFAULT_EXAMPLES_PATH,
    ) -> None:
        self._buffer = replay_buffer
        self._db = db
        self._skill_registry = skill_registry
        self._policy_state_path = policy_state_path
        self._examples_path = examples_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def update(
        self,
        episode_id: str,
        best_solution: Optional[Any],
        best_reward: float,
    ) -> None:
        if best_solution is not None:
            await self._update_prompt_examples(best_solution, best_reward)
            await self._update_skill_weights(best_solution)
        await self._update_exploration_rate(best_reward)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _update_prompt_examples(self, solution: Any, reward: float) -> None:
        """Persist high-quality solutions as few-shot prompt examples."""
        if reward < 0.8:
            return

        artifact = solution.get("artifact", "") if isinstance(solution, dict) else ""
        code = solution.get("code", "") if isinstance(solution, dict) else ""
        if not code and artifact:
            try:
                code = Path(artifact).read_text(encoding="utf-8")
            except Exception:
                pass

        if not code:
            return

        examples = self._load_examples()
        snippet = code[:500]
        if any(e.get("code_snippet") == snippet for e in examples):
            return

        examples.append(
            {
                "task_description": (
                    solution.get("description", "") if isinstance(solution, dict) else ""
                ),
                "code_snippet": snippet,
                "reward": reward,
            }
        )
        examples.sort(key=lambda e: e.get("reward", 0.0), reverse=True)
        examples = examples[:_MAX_EXAMPLES]

        try:
            self._examples_path.parent.mkdir(parents=True, exist_ok=True)
            self._examples_path.write_text(
                json.dumps(examples, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            LOGGER.warning("PolicyUpdater: failed to save examples: %s", exc)

    async def _update_skill_weights(self, solution: Any) -> None:
        """Nudge skill success_rate in PostgreSQL based on solution reward."""
        if not self._db:
            return

        skills_used: list[str] = (
            solution.get("skills_used", []) if isinstance(solution, dict) else []
        )
        reward: float = (
            solution.get("reward", 0.0) if isinstance(solution, dict) else 0.0
        )

        for skill_id in skills_used:
            try:
                await self._db.execute(
                    """
                    UPDATE skills
                    SET success_rate = CASE
                        WHEN $2 > 0.7 THEN success_rate + (1.0 - success_rate) * 0.1
                        WHEN $2 < 0.3 THEN success_rate - success_rate * 0.1
                        ELSE success_rate
                    END
                    WHERE id = $1
                    """,
                    skill_id,
                    reward,
                )
            except Exception as exc:
                LOGGER.warning(
                    "PolicyUpdater: failed to update skill %s weight: %s", skill_id, exc
                )

    async def _update_exploration_rate(self, best_reward: float) -> None:
        """Adaptive epsilon decay: reduce exploration when skills accumulate and
        average reward stays high.  Changes are applied to the NEXT episode only
        (circular-dependency guard: never mutates the currently-running episode)."""
        state = self._load_policy_state()

        current_rate: float = float(
            state.get(
                "exploration_rate",
                float(os.getenv("EXPLORATION_RATE", "0.3")),
            )
        )
        skills_count: int = int(state.get("skills_count", 0))

        # Rolling mean of per-episode best rewards
        n = int(state.get("reward_samples", 0)) + 1
        avg_reward = float(state.get("avg_reward", 0.0))
        avg_reward = avg_reward + (best_reward - avg_reward) / n

        # Decay only when we have a rich skill base and sustained high reward
        if skills_count > 20 and avg_reward > 0.7:
            current_rate = max(_MIN_EXPLORATION_RATE, current_rate * 0.95)

        state.update(
            {
                "exploration_rate": current_rate,
                "avg_reward": avg_reward,
                "reward_samples": n,
                "skills_count": skills_count,
            }
        )
        self._save_policy_state(state)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_policy_state(self) -> dict:
        try:
            if self._policy_state_path.exists():
                return json.loads(
                    self._policy_state_path.read_text(encoding="utf-8")
                )
        except Exception:
            pass
        return {}

    def _save_policy_state(self, state: dict) -> None:
        try:
            self._policy_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._policy_state_path.write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            LOGGER.warning("PolicyUpdater: failed to save policy state: %s", exc)

    def _load_examples(self) -> list:
        try:
            if self._examples_path.exists():
                return json.loads(
                    self._examples_path.read_text(encoding="utf-8")
                )
        except Exception:
            pass
        return []
