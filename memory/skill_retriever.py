"""
Skill Retriever — fetches relevant skills for a given task description.

Ranking algorithm:
  1. Get top (top_k * 2) candidates from Qdrant by vector similarity
  2. Fetch success_rate from PostgreSQL for each candidate
  3. final_score = similarity * 0.6 + success_rate * 0.4
  4. Sort by final_score descending, return top_k
  5. Update last_used_at and use_count for selected skills
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from memory.db import MemoryDB
from memory.skill import Skill
from memory.vector_store import VectorMemory

LOGGER = logging.getLogger(__name__)

_SIMILARITY_WEIGHT = 0.6
_SUCCESS_RATE_WEIGHT = 0.4


async def get_relevant_skills(
    task_description: str,
    task_embedding: list[float],
    vector_memory: VectorMemory,
    db: MemoryDB,
    top_k: int = 3,
) -> list[Skill]:
    """
    Retrieve and rank the most relevant active skills for a task.

    Falls back to an empty list if Qdrant or PostgreSQL are unavailable.
    """
    if not task_embedding or all(v == 0.0 for v in task_embedding):
        LOGGER.debug("[skill_retriever] Zero embedding — skipping retrieval")
        return []

    try:
        candidates = await vector_memory.search_similar_skills(
            task_embedding, top_k=top_k * 2
        )
    except Exception as exc:
        LOGGER.warning("[skill_retriever] Qdrant search failed: %s", exc)
        return []

    if not candidates:
        return []

    # Fetch success_rate from PostgreSQL and compute final score
    scored: list[tuple[float, dict]] = []
    for hit in candidates:
        skill_id = str(hit.get("id", ""))
        if not skill_id:
            continue
        row = await db.fetchrow(
            "SELECT success_rate, use_count, is_active FROM skills WHERE id = $1",
            skill_id,
        )
        if not row or not row.get("is_active", True):
            continue
        similarity = float(hit.get("score", 0.0))
        success_rate = float(row.get("success_rate", 0.0))
        final_score = similarity * _SIMILARITY_WEIGHT + success_rate * _SUCCESS_RATE_WEIGHT
        scored.append((final_score, {**hit, "success_rate": success_rate}))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    skills: list[Skill] = []
    for score, hit in top:
        skill = Skill(
            id=str(hit.get("id", "")),
            name=hit.get("name", ""),
            description=hit.get("description", ""),
            tags=list(hit.get("tags", [])),
            success_rate=hit.get("success_rate", 0.0),
            code_path=hit.get("code_path", ""),
        )
        skills.append(skill)

        # Update usage stats
        await _update_usage(db, skill.id)

    return skills


async def get_skill_context_for_prompt(
    task_description: str,
    task_embedding: list[float],
    vector_memory: VectorMemory,
    db: MemoryDB,
    top_k: int = 3,
) -> str:
    """
    Format top-K relevant skills as a string block for inclusion in a dev prompt.

    Returns an empty string if no relevant skills are found.
    """
    skills = await get_relevant_skills(
        task_description, task_embedding, vector_memory, db, top_k=top_k
    )
    if not skills:
        return ""

    lines = ["## Available Skills"]
    for i, skill in enumerate(skills, start=1):
        tags_str = ", ".join(skill.tags) if skill.tags else "—"
        lines.append(
            f"{i}. **{skill.name}** "
            f"(success_rate: {skill.success_rate:.2f}, tags: {tags_str})"
        )
        if skill.description:
            lines.append(f"   {skill.description}")
        if skill.code_path:
            lines.append(f"   Code: {skill.code_path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _update_usage(db: MemoryDB, skill_id: str) -> None:
    """Increment use_count and refresh last_used_at for a skill."""
    try:
        await db.execute(
            """
            UPDATE skills
            SET use_count = use_count + 1,
                last_used_at = $2
            WHERE id = $1
            """,
            skill_id,
            datetime.now(timezone.utc),
        )
    except Exception as exc:
        LOGGER.warning("[skill_retriever] Failed to update usage for %s: %s", skill_id, exc)
