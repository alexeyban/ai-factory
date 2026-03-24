"""Tests for memory/skill_retriever.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.skill_retriever import get_relevant_skills, get_skill_context_for_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(rows: list[dict] | None = None, row: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.fetch = AsyncMock(return_value=rows or [])
    db.fetchrow = AsyncMock(return_value=row)
    db.execute = AsyncMock()
    return db


def _make_vector(hits: list[dict] | None = None) -> MagicMock:
    vector = MagicMock()
    vector.search_similar_skills = AsyncMock(return_value=hits or [])
    return vector


def _fake_embedding(val: float = 0.5) -> list[float]:
    return [val] * 1536


# ---------------------------------------------------------------------------
# get_relevant_skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieval_returns_empty_on_zero_embedding():
    db = _make_db()
    vector = _make_vector()
    result = await get_relevant_skills("task desc", [0.0] * 1536, vector, db)
    assert result == []
    vector.search_similar_skills.assert_not_called()


@pytest.mark.asyncio
async def test_retrieval_returns_empty_when_no_qdrant_hits():
    db = _make_db()
    vector = _make_vector(hits=[])
    result = await get_relevant_skills("task desc", _fake_embedding(), vector, db)
    assert result == []


@pytest.mark.asyncio
async def test_retrieval_returns_top_k():
    hits = [
        {"id": "s1", "score": 0.9, "name": "skill_a", "description": "a", "tags": [], "code_path": "skills/s1.py"},
        {"id": "s2", "score": 0.8, "name": "skill_b", "description": "b", "tags": [], "code_path": "skills/s2.py"},
        {"id": "s3", "score": 0.7, "name": "skill_c", "description": "c", "tags": [], "code_path": "skills/s3.py"},
    ]
    db = _make_db(row={"success_rate": 0.8, "use_count": 2, "is_active": True})
    vector = _make_vector(hits=hits)

    result = await get_relevant_skills("task desc", _fake_embedding(), vector, db, top_k=2)
    assert len(result) <= 2


@pytest.mark.asyncio
async def test_retrieval_ranking_success_rate_raises_position():
    """
    s2 has lower Qdrant score but much higher success_rate.
    With weights 0.6/0.4, s2 should rank above s1 if the gap is large enough.
    s1: 0.6*0.6 + 0.4*0.0 = 0.36
    s2: 0.6*0.5 + 0.4*0.9 = 0.30 + 0.36 = 0.66  → s2 wins
    """
    hits = [
        {"id": "s1", "score": 0.6, "name": "skill_a", "description": "", "tags": [], "code_path": ""},
        {"id": "s2", "score": 0.5, "name": "skill_b", "description": "", "tags": [], "code_path": ""},
    ]
    db = MagicMock()
    db.execute = AsyncMock()

    async def fetchrow(query, skill_id):
        rates = {"s1": 0.0, "s2": 0.9}
        return {"success_rate": rates[skill_id], "use_count": 0, "is_active": True}

    db.fetchrow = fetchrow
    vector = _make_vector(hits=hits)

    result = await get_relevant_skills("task", _fake_embedding(), vector, db, top_k=2)
    assert len(result) == 2
    assert result[0].id == "s2"  # higher final score


@pytest.mark.asyncio
async def test_inactive_skills_excluded():
    hits = [{"id": "s1", "score": 0.9, "name": "pruned", "description": "", "tags": [], "code_path": ""}]
    db = _make_db(row={"success_rate": 0.9, "use_count": 5, "is_active": False})
    vector = _make_vector(hits=hits)
    result = await get_relevant_skills("task", _fake_embedding(), vector, db)
    assert result == []


@pytest.mark.asyncio
async def test_use_count_updated_after_retrieval():
    hits = [{"id": "s1", "score": 0.9, "name": "skill_a", "description": "", "tags": [], "code_path": ""}]
    db = _make_db(row={"success_rate": 0.5, "use_count": 1, "is_active": True})
    vector = _make_vector(hits=hits)
    await get_relevant_skills("task", _fake_embedding(), vector, db, top_k=1)
    # execute should be called to update use_count
    db.execute.assert_awaited()


@pytest.mark.asyncio
async def test_qdrant_failure_returns_empty():
    vector = MagicMock()
    vector.search_similar_skills = AsyncMock(side_effect=RuntimeError("Qdrant down"))
    db = _make_db()
    result = await get_relevant_skills("task", _fake_embedding(), vector, db)
    assert result == []


# ---------------------------------------------------------------------------
# get_skill_context_for_prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_context_empty_when_no_skills():
    vector = _make_vector(hits=[])
    db = _make_db()
    ctx = await get_skill_context_for_prompt("task", _fake_embedding(), vector, db)
    assert ctx == ""


@pytest.mark.asyncio
async def test_prompt_context_format():
    hits = [{"id": "s1", "score": 0.9, "name": "sorter", "description": "Sorts a list", "tags": ["sort"], "code_path": "skills/s1.py"}]
    db = _make_db(row={"success_rate": 0.8, "use_count": 3, "is_active": True})
    vector = _make_vector(hits=hits)
    ctx = await get_skill_context_for_prompt("task", _fake_embedding(), vector, db, top_k=1)
    assert "## Available Skills" in ctx
    assert "sorter" in ctx
    assert "skills/s1.py" in ctx
