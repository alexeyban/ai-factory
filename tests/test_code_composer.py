"""Tests for orchestrator/code_composer.py"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memory.skill import Skill
from orchestrator.code_composer import CodeComposer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill(code: str, tmp_dir: Path, skill_id: str = "s1") -> Skill:
    p = tmp_dir / f"{skill_id}.py"
    p.write_text(code, encoding="utf-8")
    return Skill(id=skill_id, name=skill_id, code_path=str(p))


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------

def test_compose_no_skills_returns_new_code():
    composer = CodeComposer()
    result = composer.compose([], "x = 1\n")
    assert result == "x = 1\n"


def test_compose_single_skill():
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(
            "import os\n\ndef helper():\n    return 42\n",
            Path(tmp),
        )
        result = CodeComposer().compose([skill], "def main():\n    pass\n")
    assert "def helper" in result
    assert "def main" in result


def test_deduplicates_imports():
    with tempfile.TemporaryDirectory() as tmp:
        s1 = _make_skill("import os\n\ndef a(): pass\n", Path(tmp), "s1")
        s2 = _make_skill("import os\nimport sys\n\ndef b(): pass\n", Path(tmp), "s2")
        result = CodeComposer().compose([s1, s2], "x = 1")
    # 'import os' should appear exactly once
    assert result.count("import os") == 1
    assert "import sys" in result


def test_preserves_skill_functions():
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(
            "def binary_search(lst, t):\n    return -1\n",
            Path(tmp),
        )
        result = CodeComposer().compose([skill], "# new code\n")
    assert "def binary_search" in result


def test_compose_deduplicates_function_definitions():
    """Two skills with same function name → only one copy in output."""
    with tempfile.TemporaryDirectory() as tmp:
        s1 = _make_skill("def helper(): return 1\n", Path(tmp), "s1")
        s2 = _make_skill("def helper(): return 2\n", Path(tmp), "s2")
        result = CodeComposer().compose([s1, s2], "x = helper()")
    assert result.count("def helper") == 1


def test_compose_fallback_on_missing_skill_file():
    """Missing file → skill is silently skipped, new_code still returned."""
    skill = Skill(id="ghost", name="ghost", code_path="/nonexistent/ghost.py")
    result = CodeComposer().compose([skill], "y = 2\n")
    assert "y = 2" in result


def test_compose_handles_syntax_error_in_skill():
    """Skill with invalid Python → fallback strips imports, rest still composed."""
    with tempfile.TemporaryDirectory() as tmp:
        bad = _make_skill("def broken(\n    pass\n", Path(tmp))
        result = CodeComposer().compose([bad], "z = 3\n")
    assert "z = 3" in result


# ---------------------------------------------------------------------------
# _extract_definitions
# ---------------------------------------------------------------------------

def test_extract_definitions_finds_functions():
    code = "import os\n\ndef foo():\n    pass\n\ndef bar():\n    return 1\n"
    defs = CodeComposer._extract_definitions(code)
    names = [CodeComposer._def_name(d) for d in defs]
    assert "foo" in names
    assert "bar" in names


def test_extract_definitions_finds_classes():
    code = "class MyClass:\n    pass\n"
    defs = CodeComposer._extract_definitions(code)
    assert any("MyClass" in d for d in defs)


# ---------------------------------------------------------------------------
# _get_strategies (imported from activities)
# ---------------------------------------------------------------------------

def test_get_strategies_all_explore_at_rate_1():
    from orchestrator.activities import _get_strategies
    strategies = _get_strategies(3, 1.0)
    assert all(s == "explore" for s in strategies)
    assert len(strategies) == 3


def test_get_strategies_mixed():
    from orchestrator.activities import _get_strategies
    strategies = _get_strategies(4, 0.5)
    assert strategies.count("explore") == 2
    assert strategies.count("exploit") == 2


def test_get_strategies_at_least_one_explore():
    from orchestrator.activities import _get_strategies
    strategies = _get_strategies(3, 0.0)
    assert "explore" in strategies


def test_get_strategies_single_candidate():
    from orchestrator.activities import _get_strategies
    strategies = _get_strategies(1, 0.3)
    assert len(strategies) == 1
