"""Tests for memory/skill_optimizer.py."""
from __future__ import annotations

import ast
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.skill import Skill
from memory.skill_optimizer import SkillOptimizer, _is_valid_python, _extract_code_from_llm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skill(
    id: str = "sk-001",
    success_rate: float = 0.4,
    use_count: int = 5,
    is_active: bool = True,
    last_optimized_at=None,
) -> Skill:
    return Skill(
        id=id,
        name="test_skill",
        description="A test skill",
        code_path=f"skills/{id}.py",
        success_rate=success_rate,
        use_count=use_count,
        is_active=is_active,
        last_optimized_at=last_optimized_at,
    )


def _optimizer(
    db=None,
    vm=None,
    llm_fn=None,
    registry=None,
    skills_dir: Path | None = None,
    prune_threshold: float = 0.3,
    sim_threshold: float = 0.9,
) -> SkillOptimizer:
    if db is None:
        db = MagicMock()
        db.fetch = AsyncMock(return_value=[])
        db.execute = AsyncMock()
    if vm is None:
        vm = MagicMock()
        vm.delete_skill = AsyncMock()
        vm.search_skills = AsyncMock(return_value=[])
        vm.upsert_skill = AsyncMock()
    if llm_fn is None:
        llm_fn = MagicMock(return_value="def optimized(): pass")
    if skills_dir is None:
        skills_dir = Path(tempfile.mkdtemp())
    return SkillOptimizer(
        db=db,
        vector_memory=vm,
        llm_fn=llm_fn,
        skill_registry=registry,
        similarity_threshold=sim_threshold,
        prune_threshold=prune_threshold,
        skills_dir=skills_dir,
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def test_is_valid_python_valid():
    assert _is_valid_python("def f(): return 1") is True


def test_is_valid_python_invalid():
    assert _is_valid_python("def f(: return 1") is False


def test_extract_code_strips_fences():
    raw = "```python\ndef f(): pass\n```"
    assert _extract_code_from_llm(raw) == "def f(): pass"


def test_extract_code_no_fences():
    raw = "def f(): pass"
    assert _extract_code_from_llm(raw) == "def f(): pass"


# ---------------------------------------------------------------------------
# prune_weak_skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prune_weak_skills_marks_inactive():
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[{"id": "sk-1", "code_path": "skills/sk-1.py"}])
    db.execute = AsyncMock()
    vm = MagicMock()
    vm.delete_skill = AsyncMock()
    registry = MagicMock()

    opt = _optimizer(db=db, vm=vm, registry=registry)
    count = await opt.prune_weak_skills(threshold=0.3)

    assert count == 1
    db.execute.assert_called_once_with(
        "UPDATE skills SET is_active = FALSE WHERE id = $1", "sk-1"
    )
    vm.delete_skill.assert_called_once_with("sk-1")
    registry.deactivate_skill.assert_called_once_with("sk-1")


@pytest.mark.asyncio
async def test_prune_returns_count():
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[
        {"id": "sk-1", "code_path": "skills/sk-1.py"},
        {"id": "sk-2", "code_path": "skills/sk-2.py"},
    ])
    db.execute = AsyncMock()
    vm = MagicMock()
    vm.delete_skill = AsyncMock()

    opt = _optimizer(db=db, vm=vm)
    count = await opt.prune_weak_skills(threshold=0.5)
    assert count == 2


@pytest.mark.asyncio
async def test_prune_db_failure_returns_zero():
    db = MagicMock()
    db.fetch = AsyncMock(side_effect=Exception("DB down"))
    opt = _optimizer(db=db)
    count = await opt.prune_weak_skills()
    assert count == 0


@pytest.mark.asyncio
async def test_prune_no_weak_skills_returns_zero():
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])
    opt = _optimizer(db=db)
    count = await opt.prune_weak_skills()
    assert count == 0


# ---------------------------------------------------------------------------
# refactor_skill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refactor_skill_updates_code():
    tmp = Path(tempfile.mkdtemp())
    skill_file = tmp / "sk-001.py"
    skill_file.write_text("def original(): pass", encoding="utf-8")

    db = MagicMock()
    db.execute = AsyncMock()
    vm = MagicMock()
    vm.upsert_skill = AsyncMock()
    llm = MagicMock(return_value="def optimized(): return 42")

    opt = _optimizer(db=db, vm=vm, llm_fn=llm, skills_dir=tmp)
    skill = _skill()
    result = await opt.refactor_skill(skill)

    assert result is not None
    assert "optimized" in skill_file.read_text()
    db.execute.assert_called()


@pytest.mark.asyncio
async def test_refactor_skill_rollback_on_invalid_python():
    tmp = Path(tempfile.mkdtemp())
    original_code = "def original(): pass"
    skill_file = tmp / "sk-001.py"
    skill_file.write_text(original_code, encoding="utf-8")

    llm = MagicMock(return_value="def broken(: syntax error")
    db = MagicMock()
    db.execute = AsyncMock()

    opt = _optimizer(db=db, llm_fn=llm, skills_dir=tmp)
    result = await opt.refactor_skill(_skill())

    assert result is None
    # File must be unchanged
    assert skill_file.read_text() == original_code


@pytest.mark.asyncio
async def test_refactor_skill_circuit_breaker():
    """Recently optimised skills (< 24h) must be skipped."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "sk-001.py").write_text("def f(): pass", encoding="utf-8")

    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    skill = _skill(last_optimized_at=recent)
    llm = MagicMock()

    opt = _optimizer(llm_fn=llm, skills_dir=tmp)
    result = await opt.refactor_skill(skill)

    assert result is None
    llm.assert_not_called()


@pytest.mark.asyncio
async def test_refactor_skill_missing_file_returns_none():
    tmp = Path(tempfile.mkdtemp())
    opt = _optimizer(skills_dir=tmp)
    result = await opt.refactor_skill(_skill(id="nonexistent"))
    assert result is None


@pytest.mark.asyncio
async def test_refactor_skill_no_change_returns_none():
    """If LLM returns identical code, refactor should return None."""
    tmp = Path(tempfile.mkdtemp())
    code = "def f(): pass"
    (tmp / "sk-001.py").write_text(code, encoding="utf-8")
    llm = MagicMock(return_value=code)
    db = MagicMock()
    db.execute = AsyncMock()

    opt = _optimizer(db=db, llm_fn=llm, skills_dir=tmp)
    result = await opt.refactor_skill(_skill())
    assert result is None


# ---------------------------------------------------------------------------
# merge_skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_skills_creates_new():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "sk-A.py").write_text("def a(): return 1", encoding="utf-8")
    (tmp / "sk-B.py").write_text("def b(): return 2", encoding="utf-8")

    merged_code = "def merged(x): return x"
    llm_response = (
        f'{{"name": "merged", "description": "merged skill", '
        f'"code": "{merged_code}", "tags": ["general"]}}'
    )
    llm = MagicMock(return_value=llm_response)
    db = MagicMock()
    db.execute = AsyncMock()
    vm = MagicMock()
    vm.delete_skill = AsyncMock()
    vm.upsert_skill = AsyncMock()
    registry = MagicMock()

    opt = _optimizer(db=db, vm=vm, llm_fn=llm, registry=registry, skills_dir=tmp)
    skills = [_skill(id="sk-A"), _skill(id="sk-B")]
    result = await opt.merge_skills(skills)

    assert result is not None
    assert result.name == "merged"
    # Old skills deactivated
    db.execute.assert_any_call(
        "UPDATE skills SET is_active = FALSE WHERE id = $1", "sk-A"
    )
    db.execute.assert_any_call(
        "UPDATE skills SET is_active = FALSE WHERE id = $1", "sk-B"
    )
    registry.deactivate_skill.assert_any_call("sk-A")


@pytest.mark.asyncio
async def test_merge_skills_rejects_invalid_python():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "sk-A.py").write_text("def a(): pass", encoding="utf-8")
    (tmp / "sk-B.py").write_text("def b(): pass", encoding="utf-8")

    bad_code = "def broken(:"
    llm_response = f'{{"name": "x", "description": "", "code": "{bad_code}", "tags": []}}'
    llm = MagicMock(return_value=llm_response)
    db = MagicMock()
    db.execute = AsyncMock()

    opt = _optimizer(db=db, llm_fn=llm, skills_dir=tmp)
    result = await opt.merge_skills([_skill(id="sk-A"), _skill(id="sk-B")])
    assert result is None


@pytest.mark.asyncio
async def test_merge_requires_at_least_two():
    opt = _optimizer()
    result = await opt.merge_skills([_skill()])
    assert result is None


# ---------------------------------------------------------------------------
# _find_similar_clusters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_similar_clusters_groups_correctly():
    """Two skills that are similar to each other form one cluster."""
    sk_a = _skill(id="sk-A")
    sk_b = _skill(id="sk-B")

    db = MagicMock()
    db.fetch = AsyncMock(return_value=[
        {"id": "sk-A", "name": "a", "description": "", "code_path": "",
         "success_rate": 0.8, "use_count": 3, "tags": [],
         "last_optimized_at": None, "is_active": True},
        {"id": "sk-B", "name": "b", "description": "", "code_path": "",
         "success_rate": 0.7, "use_count": 2, "tags": [],
         "last_optimized_at": None, "is_active": True},
    ])
    vm = MagicMock()
    # sk-A finds sk-B as neighbour; sk-B finds sk-A
    async def mock_search(query_vector, skill_id=None, limit=10, score_threshold=0.9):
        if skill_id == "sk-A":
            return [{"id": "sk-B", "score": 0.95}]
        if skill_id == "sk-B":
            return [{"id": "sk-A", "score": 0.95}]
        return []

    vm.search_skills = mock_search

    opt = _optimizer(db=db, vm=vm)
    clusters = await opt._find_similar_clusters()

    assert len(clusters) == 1
    ids = {s.id for s in clusters[0]}
    assert ids == {"sk-A", "sk-B"}


# ---------------------------------------------------------------------------
# run_optimization_cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_optimization_cycle_runs_all():
    """run_optimization_cycle calls refactor, merge, and prune."""
    opt = _optimizer()
    # Patch all three internal methods
    opt._refactor_weak_skills = AsyncMock(return_value=2)
    opt._merge_similar_skills = AsyncMock(return_value=1)
    opt.prune_weak_skills = AsyncMock(return_value=3)

    stats = await opt.run_optimization_cycle(episode_count=10)

    opt._refactor_weak_skills.assert_awaited_once()
    opt._merge_similar_skills.assert_awaited_once()
    opt.prune_weak_skills.assert_awaited_once()
    assert stats == {"refactored": 2, "merged": 1, "pruned": 3}
