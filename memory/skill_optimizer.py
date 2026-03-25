"""Skill Optimizer — refactors, merges, and prunes the skill base.

Called periodically (every SKILL_OPTIMIZE_EVERY_N episodes) via
skill_optimization_activity so the skill library self-improves over time.

Three mechanisms:
    refactor  — LLM rewrites a weak skill; only applied if AST is valid
    merge     — LLM combines two or more similar skills into one generalised skill
    prune     — soft-deletes skills below a success_rate threshold
"""
from __future__ import annotations

import ast
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)

_REFACTOR_SUCCESS_THRESHOLD = 0.5   # refactor if success_rate < this
_REFACTOR_MIN_USE_COUNT = 3         # skip rarely-used skills
_DEFAULT_PRUNE_THRESHOLD = float(os.getenv("SKILL_PRUNE_THRESHOLD", "0.3"))
_DEFAULT_SIMILARITY_THRESHOLD = float(os.getenv("SKILL_SIMILARITY_THRESHOLD", "0.9"))
_SKILLS_DIR = Path(os.getenv("SKILLS_DIR", "skills"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_valid_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _extract_code_from_llm(raw: str) -> str:
    """Strip markdown fences from LLM response."""
    lines = raw.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# SkillOptimizer
# ---------------------------------------------------------------------------

class SkillOptimizer:
    """Optimises the skill library between learning episodes."""

    def __init__(
        self,
        db,
        vector_memory,
        llm_fn: Callable[..., str],
        skill_registry,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        prune_threshold: float = _DEFAULT_PRUNE_THRESHOLD,
        skills_dir: Path = _SKILLS_DIR,
    ) -> None:
        self._db = db
        self._vm = vector_memory
        self._llm = llm_fn
        self._registry = skill_registry
        self._sim_threshold = similarity_threshold
        self._prune_threshold = prune_threshold
        self._skills_dir = skills_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_optimization_cycle(self, episode_count: int) -> dict:
        """Run all three optimisation passes and return aggregate stats."""
        LOGGER.info(
            "[skill_optimizer] Starting optimization cycle (episode=%d)", episode_count
        )
        stats: dict[str, int] = {}
        stats["refactored"] = await self._refactor_weak_skills()
        stats["merged"] = await self._merge_similar_skills()
        stats["pruned"] = await self.prune_weak_skills()
        LOGGER.info("[skill_optimizer] Cycle complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Prune
    # ------------------------------------------------------------------

    async def prune_weak_skills(self, threshold: Optional[float] = None) -> int:
        """Soft-delete skills with success_rate below threshold.

        Marks is_active=FALSE in PostgreSQL, removes from Qdrant, and
        deactivates in registry.json.  The .py files are intentionally kept
        for audit / potential recovery.

        Returns the number of pruned skills.
        """
        thr = threshold if threshold is not None else self._prune_threshold
        try:
            rows = await self._db.fetch(
                """
                SELECT id, code_path FROM skills
                WHERE success_rate < $1 AND is_active = TRUE
                """,
                thr,
            )
        except Exception as exc:
            LOGGER.warning("[skill_optimizer] prune: DB fetch failed: %s", exc)
            return 0

        count = 0
        for row in rows:
            skill_id = row["id"]
            try:
                await self._db.execute(
                    "UPDATE skills SET is_active = FALSE WHERE id = $1", skill_id
                )
                try:
                    await self._vm.delete_skill(skill_id)
                except Exception:
                    pass  # Qdrant delete is best-effort
                if self._registry:
                    self._registry.deactivate_skill(skill_id)
                count += 1
                LOGGER.info("[skill_optimizer] Pruned skill %s", skill_id)
            except Exception as exc:
                LOGGER.warning(
                    "[skill_optimizer] prune: failed for %s: %s", skill_id, exc
                )

        return count

    # ------------------------------------------------------------------
    # Refactor
    # ------------------------------------------------------------------

    async def refactor_skill(self, skill) -> Optional[Any]:
        """LLM-rewrite a single skill; validate AST before applying.

        Returns the updated Skill on success, None on failure / no change.
        Adds a circuit-breaker: skip if last_optimized_at is within 24 h.
        """
        from memory.skill import Skill as SkillModel

        code_path = self._skills_dir / f"{skill.id}.py"
        if not code_path.exists():
            return None

        # Circuit-breaker: don't re-optimise a recently touched skill
        if skill.last_optimized_at is not None:
            age_h = (
                datetime.now(timezone.utc) - skill.last_optimized_at
            ).total_seconds() / 3600
            if age_h < 24:
                return None

        original_code = code_path.read_text(encoding="utf-8")

        system = (
            "You are a Python expert. Optimize the provided function for clarity "
            "and performance. Return only the improved Python code — no explanations, "
            "no markdown fences."
        )
        user = f"Optimize this Python function:\n\n{original_code}"

        try:
            raw = self._llm(system, user)
        except Exception as exc:
            LOGGER.warning("[skill_optimizer] refactor LLM call failed: %s", exc)
            return None

        new_code = _extract_code_from_llm(raw)

        if not _is_valid_python(new_code):
            LOGGER.warning(
                "[skill_optimizer] refactor produced invalid Python for skill %s — skipping",
                skill.id,
            )
            return None

        if new_code.strip() == original_code.strip():
            return None  # no change

        # Apply
        code_path.write_text(new_code, encoding="utf-8")

        now = datetime.now(timezone.utc)
        try:
            await self._db.execute(
                """
                UPDATE skills
                SET last_optimized_at = $2
                WHERE id = $1
                """,
                skill.id,
                now,
            )
        except Exception as exc:
            LOGGER.warning(
                "[skill_optimizer] refactor: DB update failed for %s: %s", skill.id, exc
            )

        # Re-embed with the updated code (best-effort)
        try:
            embed_text = f"{skill.description} {' '.join(skill.tags)} {new_code[:300]}"
            embedding = SkillModel.embed_text(skill) if hasattr(skill, "embed_text") else []
            if self._vm and embedding:
                await self._vm.upsert_skill(skill, embedding)
        except Exception:
            pass

        LOGGER.info("[skill_optimizer] Refactored skill %s", skill.id)
        skill.last_optimized_at = now
        return skill

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    async def merge_skills(self, skills: list) -> Optional[Any]:
        """Merge two or more similar skills into one generalised skill.

        1. LLM combines all code into a single general implementation
        2. AST-validate the result
        3. Create new skill file + DB row
        4. Deactivate old skills (is_active=FALSE + Qdrant delete)
        5. Update registry.json
        """
        from memory.skill import Skill as SkillModel

        if len(skills) < 2:
            return None

        codes: list[str] = []
        for s in skills:
            p = self._skills_dir / f"{s.id}.py"
            if p.exists():
                codes.append(p.read_text(encoding="utf-8"))

        if len(codes) < 2:
            return None

        combined = "\n\n# --- skill ---\n\n".join(codes)
        system = (
            "You are a Python expert. Merge the following similar Python functions "
            "into one general implementation that covers all use cases.\n"
            "Return a JSON object with keys: name (str), description (str), "
            "code (str), tags (list[str]).\n"
            "No markdown fences around the JSON."
        )
        user = f"Merge these Python functions:\n\n{combined}"

        try:
            raw = self._llm(system, user)
        except Exception as exc:
            LOGGER.warning("[skill_optimizer] merge LLM call failed: %s", exc)
            return None

        # Parse JSON response
        try:
            # Strip markdown fences if present
            clean = raw.strip()
            if clean.startswith("```"):
                clean = "\n".join(clean.splitlines()[1:])
            if clean.endswith("```"):
                clean = "\n".join(clean.splitlines()[:-1])
            merged_data = json.loads(clean)
        except json.JSONDecodeError:
            LOGGER.warning("[skill_optimizer] merge: failed to parse LLM JSON")
            return None

        new_code = merged_data.get("code", "")
        if not new_code or not _is_valid_python(new_code):
            LOGGER.warning("[skill_optimizer] merge: invalid Python from LLM — skipping")
            return None

        # Create new skill
        new_id = str(uuid.uuid4())
        new_path = self._skills_dir / f"{new_id}.py"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(new_code, encoding="utf-8")

        new_skill = SkillModel(
            id=new_id,
            name=merged_data.get("name", "merged_skill"),
            description=merged_data.get("description", ""),
            code_path=str(new_path.relative_to(Path("."))),
            tags=list(merged_data.get("tags", [])),
            success_rate=sum(s.success_rate for s in skills) / len(skills),
            use_count=sum(s.use_count for s in skills),
            is_active=True,
        )

        try:
            await self._db.execute(
                """
                INSERT INTO skills (id, name, description, code_path,
                    success_rate, use_count, tags, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
                """,
                new_skill.id, new_skill.name, new_skill.description,
                new_skill.code_path, new_skill.success_rate,
                new_skill.use_count, new_skill.tags,
            )
        except Exception as exc:
            LOGGER.warning("[skill_optimizer] merge: DB insert failed: %s", exc)
            new_path.unlink(missing_ok=True)
            return None

        # Deactivate old skills
        for s in skills:
            try:
                await self._db.execute(
                    "UPDATE skills SET is_active = FALSE WHERE id = $1", s.id
                )
                await self._vm.delete_skill(s.id)
                if self._registry:
                    self._registry.deactivate_skill(s.id)
            except Exception as exc:
                LOGGER.warning(
                    "[skill_optimizer] merge: deactivation failed for %s: %s", s.id, exc
                )

        # Register new skill
        if self._registry:
            self._registry.add_skill(new_skill)

        # Upsert to Qdrant (best-effort)
        try:
            await self._vm.upsert_skill(new_skill, [])
        except Exception:
            pass

        LOGGER.info(
            "[skill_optimizer] Merged %d skills into %s", len(skills), new_id
        )
        return new_skill

    # ------------------------------------------------------------------
    # Internal passes
    # ------------------------------------------------------------------

    async def _refactor_weak_skills(self) -> int:
        """Refactor skills with low success_rate and enough usage."""
        try:
            rows = await self._db.fetch(
                """
                SELECT id, name, description, code_path,
                       success_rate, use_count, tags,
                       last_optimized_at, is_active
                FROM skills
                WHERE success_rate < $1
                  AND use_count >= $2
                  AND is_active = TRUE
                """,
                _REFACTOR_SUCCESS_THRESHOLD,
                _REFACTOR_MIN_USE_COUNT,
            )
        except Exception as exc:
            LOGGER.warning("[skill_optimizer] refactor fetch failed: %s", exc)
            return 0

        count = 0
        for row in rows:
            skill = _row_to_skill(row)
            result = await self.refactor_skill(skill)
            if result:
                count += 1
        return count

    async def _merge_similar_skills(self) -> int:
        """Find clusters of similar skills and merge each cluster."""
        clusters = await self._find_similar_clusters()
        count = 0
        for cluster in clusters:
            if len(cluster) >= 2:
                merged = await self.merge_skills(cluster)
                if merged:
                    count += 1
        return count

    async def _find_similar_clusters(self) -> list[list]:
        """Build clusters of skills with cosine similarity > threshold.

        Uses a greedy connected-components approach:
        - For each active skill, query Qdrant for neighbours
        - Group transitively connected skills into clusters
        - Each skill appears in at most one cluster
        """
        try:
            rows = await self._db.fetch(
                "SELECT id, name, description, code_path, success_rate, "
                "use_count, tags, last_optimized_at, is_active "
                "FROM skills WHERE is_active = TRUE"
            )
        except Exception as exc:
            LOGGER.warning("[skill_optimizer] cluster fetch failed: %s", exc)
            return []

        skills = [_row_to_skill(r) for r in rows]
        if len(skills) < 2:
            return []

        # Build adjacency: skill_id → set of similar skill_ids
        adjacency: dict[str, set[str]] = {s.id: set() for s in skills}
        id_to_skill = {s.id: s for s in skills}

        for skill in skills:
            try:
                neighbours = await self._vm.search_skills(
                    query_vector=[],   # will use stored embedding via ID
                    skill_id=skill.id,
                    limit=10,
                    score_threshold=self._sim_threshold,
                )
                for nb in neighbours:
                    nb_id = nb.get("id", "")
                    if nb_id and nb_id != skill.id and nb_id in adjacency:
                        adjacency[skill.id].add(nb_id)
                        adjacency[nb_id].add(skill.id)
            except Exception:
                continue

        # Connected components via BFS
        visited: set[str] = set()
        clusters: list[list] = []
        for skill in skills:
            if skill.id in visited:
                continue
            component: list = []
            queue = [skill.id]
            while queue:
                sid = queue.pop()
                if sid in visited:
                    continue
                visited.add(sid)
                if sid in id_to_skill:
                    component.append(id_to_skill[sid])
                queue.extend(adjacency.get(sid, set()) - visited)
            if len(component) >= 2:
                clusters.append(component)

        return clusters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_skill(row: dict):
    from memory.skill import Skill

    def _dt(val):
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        try:
            return datetime.fromisoformat(str(val))
        except Exception:
            return None

    return Skill(
        id=row["id"],
        name=row.get("name", ""),
        description=row.get("description", ""),
        code_path=row.get("code_path", ""),
        success_rate=float(row.get("success_rate", 0.0)),
        use_count=int(row.get("use_count", 0)),
        tags=list(row.get("tags") or []),
        last_optimized_at=_dt(row.get("last_optimized_at")),
        is_active=bool(row.get("is_active", True)),
    )
