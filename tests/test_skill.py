"""Tests for memory/skill.py"""
from __future__ import annotations

from datetime import datetime, timezone

from memory.skill import Skill


def test_skill_default_id_is_uuid():
    s = Skill()
    assert len(s.id) == 36
    assert s.id.count("-") == 4


def test_skill_to_dict_roundtrip():
    s = Skill(
        name="binary_search",
        description="Efficient binary search implementation",
        tags=["algorithm", "search"],
        success_rate=0.85,
        use_count=3,
        code_path="skills/abc.py",
    )
    d = s.to_dict()
    assert d["name"] == "binary_search"
    assert d["tags"] == ["algorithm", "search"]
    assert d["success_rate"] == 0.85
    assert "embedding" not in d  # not serialised


def test_skill_from_dict():
    data = {
        "id": "test-id-001",
        "name": "sort_helper",
        "description": "A helper for sorting",
        "code_path": "skills/test-id-001.py",
        "success_rate": 0.7,
        "use_count": 5,
        "tags": ["sort", "util"],
        "created_at": "2026-03-24T00:00:00+00:00",
        "last_used_at": None,
        "is_active": True,
    }
    s = Skill.from_dict(data)
    assert s.id == "test-id-001"
    assert s.name == "sort_helper"
    assert s.success_rate == 0.7
    assert s.tags == ["sort", "util"]
    assert s.last_used_at is None


def test_skill_from_dict_missing_fields_uses_defaults():
    s = Skill.from_dict({})
    assert s.name == ""
    assert s.success_rate == 0.0
    assert s.tags == []
    assert s.is_active is True


def test_skill_embed_text_combines_description_and_tags():
    s = Skill(description="sort list in place", tags=["sort", "list"])
    assert s.embed_text() == "sort list in place sort list"


def test_skill_embed_text_empty():
    s = Skill()
    assert s.embed_text() == ""
