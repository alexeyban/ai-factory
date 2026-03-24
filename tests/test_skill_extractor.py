"""Tests for memory/skill_extractor.py"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.skill_extractor import SkillExtractor, _EXTRACT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_LLM_RESPONSE = json.dumps({
    "name": "binary_search",
    "description": "Binary search over a sorted list",
    "code": "def binary_search(lst, target):\n    lo, hi = 0, len(lst)-1\n    while lo <= hi:\n        mid = (lo+hi)//2\n        if lst[mid] == target: return mid\n        elif lst[mid] < target: lo = mid+1\n        else: hi = mid-1\n    return -1",
    "tags": ["search", "algorithm"],
})

_SAMPLE_CODE = "def add(a, b):\n    return a + b\n"


def _make_extractor(llm_response: str = _VALID_LLM_RESPONSE,
                    skills_dir: Path | None = None) -> SkillExtractor:
    db = MagicMock()
    db.execute = AsyncMock()

    vector = MagicMock()
    vector.upsert_skill = AsyncMock()

    registry = MagicMock()
    registry.add_skill = MagicMock()

    llm_fn = MagicMock(return_value=llm_response)

    extractor = SkillExtractor(
        llm_fn=llm_fn,
        vector_memory=vector,
        db=db,
        registry=registry,
    )
    if skills_dir:
        extractor._SKILLS_DIR = skills_dir  # type: ignore[attr-defined]

    return extractor


# ---------------------------------------------------------------------------
# extract_from_solution — success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_from_solution_success():
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        # Patch the module-level _SKILLS_DIR
        with patch("memory.skill_extractor._SKILLS_DIR", skills_dir):
            with patch("memory.skill_extractor.VectorMemory.embed_text",
                       new=AsyncMock(return_value=[0.1] * 1536)):
                extractor = _make_extractor()
                skill = await extractor.extract_from_solution(
                    task_id="T001",
                    episode_id="ep_001",
                    code=_SAMPLE_CODE,
                )

    assert skill is not None
    assert skill.name == "binary_search"
    assert "search" in skill.tags


@pytest.mark.asyncio
async def test_extract_from_solution_saves_file():
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        with patch("memory.skill_extractor._SKILLS_DIR", skills_dir):
            with patch("memory.skill_extractor.VectorMemory.embed_text",
                       new=AsyncMock(return_value=[0.0] * 1536)):
                extractor = _make_extractor()
                skill = await extractor.extract_from_solution("T001", "ep_001", _SAMPLE_CODE)

        if skill:
            skill_file = skills_dir / f"{skill.id}.py"
            assert skill_file.exists()
            assert "binary_search" in skill_file.read_text()


@pytest.mark.asyncio
async def test_kafka_published_on_extraction():
    producer = MagicMock()
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        with patch("memory.skill_extractor._SKILLS_DIR", skills_dir):
            with patch("memory.skill_extractor.VectorMemory.embed_text",
                       new=AsyncMock(return_value=[0.0] * 1536)):
                db = MagicMock()
                db.execute = AsyncMock()
                vector = MagicMock()
                vector.upsert_skill = AsyncMock()
                registry = MagicMock()
                registry.add_skill = MagicMock()
                extractor = SkillExtractor(
                    llm_fn=MagicMock(return_value=_VALID_LLM_RESPONSE),
                    vector_memory=vector,
                    db=db,
                    kafka_producer=producer,
                    registry=registry,
                )
                await extractor.extract_from_solution("T001", "ep_001", _SAMPLE_CODE)

    producer.send.assert_called_once()
    topic = producer.send.call_args[0][0]
    assert topic == "skill.extracted"


# ---------------------------------------------------------------------------
# extract_from_solution — failure / edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_handles_invalid_llm_json():
    extractor = _make_extractor(llm_response="not valid json at all")
    skill = await extractor.extract_from_solution("T001", "ep_001", _SAMPLE_CODE)
    assert skill is None


@pytest.mark.asyncio
async def test_extract_handles_skip_response():
    extractor = _make_extractor(llm_response=json.dumps({"skip": True}))
    skill = await extractor.extract_from_solution("T001", "ep_001", _SAMPLE_CODE)
    assert skill is None


@pytest.mark.asyncio
async def test_extract_handles_empty_code():
    extractor = _make_extractor()
    skill = await extractor.extract_from_solution("T001", "ep_001", "")
    assert skill is None


@pytest.mark.asyncio
async def test_extract_handles_llm_exception():
    db = MagicMock()
    db.execute = AsyncMock()
    vector = MagicMock()
    vector.upsert_skill = AsyncMock()
    registry = MagicMock()
    extractor = SkillExtractor(
        llm_fn=MagicMock(side_effect=RuntimeError("LLM down")),
        vector_memory=vector,
        db=db,
        registry=registry,
    )
    skill = await extractor.extract_from_solution("T001", "ep_001", _SAMPLE_CODE)
    assert skill is None


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_valid_json():
    result = SkillExtractor._parse_llm_response(_VALID_LLM_RESPONSE)
    assert result is not None
    assert result["name"] == "binary_search"


def test_parse_json_in_markdown_fences():
    raw = f"```json\n{_VALID_LLM_RESPONSE}\n```"
    result = SkillExtractor._parse_llm_response(raw)
    assert result is not None


def test_parse_returns_none_on_skip():
    result = SkillExtractor._parse_llm_response(json.dumps({"skip": True}))
    assert result is None


def test_parse_returns_none_on_garbage():
    result = SkillExtractor._parse_llm_response("just some text with no JSON")
    assert result is None
