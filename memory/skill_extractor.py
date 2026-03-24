"""
Skill Extractor — derives reusable code patterns from successful dev solutions.

Pipeline per solution:
  1. Call LLM to extract a named pattern + tags from the solution code
  2. Get embedding for (description + tags) text
  3. Save code to skills/<skill_id>.py
  4. Update skills/registry.json (SkillRegistry)
  5. Persist metadata to PostgreSQL (skills table)
  6. Upsert embedding in Qdrant (VectorMemory)
  7. Publish to Kafka topic skill.extracted (fire-and-forget)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.db import MemoryDB
from memory.skill import Skill
from memory.vector_store import VectorMemory

LOGGER = logging.getLogger(__name__)

SKILL_EXTRACTED_TOPIC = "skill.extracted"
_SKILLS_DIR = Path(__file__).parent.parent / "skills"

_EXTRACT_SYSTEM_PROMPT = """\
You are a code pattern analyser. Given Python source code from a successful \
implementation, extract the core reusable pattern.

Respond ONLY with valid JSON — no markdown fences, no commentary:
{
  "name": "<short snake_case name>",
  "description": "<one sentence describing what the pattern does>",
  "code": "<the reusable code snippet>",
  "tags": ["<tag1>", "<tag2>"]
}

If no reusable pattern can be identified, return {"skip": true}.
"""


class SkillExtractor:
    """
    Extracts, stores, and indexes a reusable skill from a dev solution.

    Parameters
    ----------
    llm_fn:
        Callable(system_prompt, user_prompt) -> str.
        Pass ``shared.llm.call_llm`` in production.
    vector_memory:
        VectorMemory instance for Qdrant embedding storage.
    db:
        MemoryDB instance for PostgreSQL persistence.
    kafka_producer:
        Optional Kafka producer; publishing failures are swallowed silently.
    registry:
        SkillRegistry to update; defaults to the project-level registry.
    """

    def __init__(
        self,
        llm_fn: Any,
        vector_memory: VectorMemory,
        db: MemoryDB,
        kafka_producer: Any | None = None,
        registry: Any | None = None,
    ) -> None:
        self._llm = llm_fn
        self._vector = vector_memory
        self._db = db
        self._kafka = kafka_producer

        if registry is None:
            from skills import SkillRegistry
            self._registry = SkillRegistry()
        else:
            self._registry = registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract_from_solution(
        self,
        task_id: str,
        episode_id: str,
        code: str,
    ) -> Skill | None:
        """
        Main entry point.

        Returns the new Skill on success, or None if extraction was skipped /
        failed.
        """
        if not code or not code.strip():
            LOGGER.debug("[skill_extractor] Empty code — skipping")
            return None

        pattern = await self._call_llm_for_pattern(code)
        if not pattern:
            return None

        skill = Skill(
            name=pattern["name"],
            description=pattern["description"],
            tags=pattern.get("tags", []),
        )

        # Embedding
        skill.embedding = await self._get_embedding(skill.embed_text())

        # Persist code file
        skill.code_path = self._save_skill_file(skill.id, pattern["code"])

        # Update registry.json
        self._registry.add_skill(skill)

        # PostgreSQL
        await self._db.execute(
            """
            INSERT INTO skills (id, name, description, code_path,
                                success_rate, use_count, tags, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO NOTHING
            """,
            skill.id,
            skill.name,
            skill.description,
            skill.code_path,
            skill.success_rate,
            skill.use_count,
            skill.tags,
            skill.is_active,
        )

        # Qdrant
        await self._vector.upsert_skill(
            skill.id,
            skill.embedding,
            {
                "name": skill.name,
                "description": skill.description,
                "tags": skill.tags,
                "success_rate": skill.success_rate,
            },
        )

        # Kafka
        self._publish(skill, episode_id=episode_id, task_id=task_id)

        LOGGER.info(
            "[skill_extractor] Extracted skill %r (id=%s, tags=%s)",
            skill.name, skill.id, skill.tags,
        )
        return skill

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _call_llm_for_pattern(self, code: str) -> dict | None:
        """
        Call the LLM to extract a pattern dict from code.

        Returns None if the LLM says to skip or returns unparseable output.
        """
        user_prompt = f"Code:\n```python\n{code}\n```"
        try:
            raw = self._llm(_EXTRACT_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            LOGGER.warning("[skill_extractor] LLM call failed: %s", exc)
            return None

        return self._parse_llm_response(raw)

    @staticmethod
    def _parse_llm_response(raw: str) -> dict | None:
        """
        Parse JSON from a (potentially noisy) LLM response.

        Tries direct JSON parse first; falls back to extracting the first
        {...} block via regex.
        """
        if not raw:
            return None

        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()

        for candidate in [cleaned, raw]:
            try:
                data = json.loads(candidate)
                if data.get("skip"):
                    return None
                if data.get("name") and data.get("code"):
                    return data
            except (json.JSONDecodeError, AttributeError):
                pass

        # Regex fallback: extract first {...} block
        match = re.search(r"\{[\s\S]+\}", raw)
        if match:
            try:
                data = json.loads(match.group())
                if data.get("skip"):
                    return None
                if data.get("name") and data.get("code"):
                    return data
            except json.JSONDecodeError:
                pass

        LOGGER.warning(
            "[skill_extractor] Could not parse LLM response as JSON "
            "(first 200 chars): %s", raw[:200]
        )
        return None

    async def _get_embedding(self, text: str) -> list[float]:
        """Return embedding via VectorMemory.embed_text with fallback."""
        try:
            return await VectorMemory.embed_text(text)
        except Exception as exc:
            LOGGER.warning("[skill_extractor] Embedding failed: %s", exc)
            from memory.vector_store import VECTOR_DIM
            return [0.0] * VECTOR_DIM

    def _save_skill_file(self, skill_id: str, code: str) -> str:
        """
        Write code to skills/<skill_id>.py and return the relative path string.
        """
        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill_file = _SKILLS_DIR / f"{skill_id}.py"
        skill_file.write_text(code, encoding="utf-8")
        return f"skills/{skill_id}.py"

    def _publish(self, skill: Skill, episode_id: str, task_id: str) -> None:
        if self._kafka is None:
            return
        payload = {
            "skill_id": skill.id,
            "name": skill.name,
            "tags": skill.tags,
            "episode_id": episode_id,
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._kafka.send(SKILL_EXTRACTED_TOPIC, payload)
        except Exception as exc:
            LOGGER.warning("[skill_extractor] Kafka publish failed: %s", exc)
