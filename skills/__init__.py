"""
Skill registry — file-system cache of all extracted skills.

registry.json acts as a lightweight index so callers can list / look up
skills without hitting PostgreSQL. The PostgreSQL table is the source of
truth; registry.json is rebuilt on demand if it drifts.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.skill import Skill

LOGGER = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent
_REGISTRY_PATH = _SKILLS_DIR / "registry.json"


class SkillRegistry:
    """File-system skill registry backed by registry.json."""

    def __init__(self, registry_path: Path = _REGISTRY_PATH) -> None:
        self._registry_path = registry_path
        self._skills_dir = registry_path.parent

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self) -> dict[str, dict]:
        """Return the skills index from registry.json."""
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
            return data.get("skills", {})
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not load skill registry: %s", exc)
            return {}

    def save(self, skills: dict[str, dict]) -> None:
        """Overwrite registry.json with the supplied skills index."""
        payload = {
            "version": "1.0",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "skills": skills,
        }
        self._registry_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_skill(self, skill: Skill) -> None:
        """Add or update a skill entry in registry.json."""
        skills = self.load()
        skills[skill.id] = {
            "name": skill.name,
            "description": skill.description,
            "tags": skill.tags,
            "success_rate": skill.success_rate,
            "use_count": skill.use_count,
            "code_path": skill.code_path,
            "created_at": skill.created_at.isoformat(),
            "is_active": skill.is_active,
        }
        self.save(skills)
        LOGGER.debug("SkillRegistry: added skill %s (%s)", skill.id, skill.name)

    def remove_skill(self, skill_id: str) -> None:
        """Remove a skill from registry.json (does not delete the .py file)."""
        skills = self.load()
        if skill_id in skills:
            del skills[skill_id]
            self.save(skills)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_skill_code(self, skill_id: str) -> str:
        """Read and return the source code for the given skill_id."""
        skills = self.load()
        entry = skills.get(skill_id)
        if not entry:
            raise KeyError(f"Skill {skill_id!r} not found in registry")
        code_path = self._skills_dir.parent / entry["code_path"]
        if not code_path.exists():
            raise FileNotFoundError(f"Skill file not found: {code_path}")
        return code_path.read_text(encoding="utf-8")

    def list_active_skills(self) -> list[dict[str, Any]]:
        """Return all active skill entries as a list."""
        skills = self.load()
        return [
            {"id": sid, **meta}
            for sid, meta in skills.items()
            if meta.get("is_active", True)
        ]
