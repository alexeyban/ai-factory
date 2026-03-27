"""
Tests for _parse_multi_file_output in orchestrator/activities.py.

Covers: plain headers, code-fence-wrapped blocks, empty/truncated content,
opening-fence stripping, and the truncation guard (skip empty files).
"""
from __future__ import annotations

import pytest

from orchestrator.activities import _parse_multi_file_output


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------

def test_no_headers_returns_empty():
    assert _parse_multi_file_output("def foo(): pass") == []


def test_single_file_no_fence():
    raw = "=== FILE: calc.py ===\ndef add(a, b):\n    return a + b\n"
    result = _parse_multi_file_output(raw)
    assert len(result) == 1
    path, content = result[0]
    assert path == "calc.py"
    assert "def add" in content


def test_two_files_plain():
    raw = (
        "=== FILE: calc.py ===\n"
        "def add(a, b):\n    return a + b\n\n"
        "=== FILE: tests/test_calc.py ===\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    )
    result = _parse_multi_file_output(raw)
    assert len(result) == 2
    assert result[0][0] == "calc.py"
    assert result[1][0] == "tests/test_calc.py"
    assert "def add" in result[0][1]
    assert "def test_add" in result[1][1]


# ---------------------------------------------------------------------------
# Code fence stripping
# ---------------------------------------------------------------------------

def test_strips_opening_python_fence():
    raw = (
        "=== FILE: calc.py ===\n"
        "```python\n"
        "def add(a, b):\n    return a + b\n"
        "```\n"
    )
    result = _parse_multi_file_output(raw)
    assert len(result) == 1
    content = result[0][1]
    assert not content.startswith("```")
    assert "def add" in content


def test_strips_opening_fence_second_file():
    """Regression: second file's opening code fence must also be stripped."""
    raw = (
        "=== FILE: calc.py ===\n"
        "```python\n"
        "def add(a, b):\n    return a + b\n"
        "```\n\n"
        "=== FILE: tests/test_calc.py ===\n"
        "```python\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
        "```\n"
    )
    result = _parse_multi_file_output(raw)
    assert len(result) == 2
    for _path, content in result:
        assert not content.startswith("```"), f"Opening fence not stripped in: {content[:40]!r}"


def test_strips_plain_fence_no_language():
    raw = (
        "=== FILE: util.py ===\n"
        "```\n"
        "x = 1\n"
        "```\n"
    )
    result = _parse_multi_file_output(raw)
    assert len(result) == 1
    assert not result[0][1].startswith("```")
    assert "x = 1" in result[0][1]


# ---------------------------------------------------------------------------
# Truncation guard — empty content skipped
# ---------------------------------------------------------------------------

def test_empty_second_file_skipped():
    """If LLM truncates and the second file has no content, it must be skipped."""
    raw = (
        "=== FILE: calc.py ===\n"
        "def add(a, b):\n    return a + b\n\n"
        "=== FILE: tests/test_calc.py ===\n"
        # no content for the second file
    )
    result = _parse_multi_file_output(raw)
    # Only the first file with real content should be returned
    assert len(result) == 1
    assert result[0][0] == "calc.py"


def test_empty_only_fence_skipped():
    """A file block that contains only ``` (empty code fence) must be skipped."""
    raw = (
        "=== FILE: calc.py ===\n"
        "def add(a, b):\n    return a + b\n\n"
        "=== FILE: tests/test_calc.py ===\n"
        "```python\n"
        "```\n"
    )
    result = _parse_multi_file_output(raw)
    assert len(result) == 1
    assert result[0][0] == "calc.py"


def test_whitespace_only_content_skipped():
    raw = (
        "=== FILE: a.py ===\n"
        "x = 1\n\n"
        "=== FILE: b.py ===\n"
        "   \n\n"
    )
    result = _parse_multi_file_output(raw)
    assert len(result) == 1
    assert result[0][0] == "a.py"


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------

def test_nested_path_preserved():
    raw = "=== FILE: src/lib/util.py ===\ndef helper(): pass\n"
    result = _parse_multi_file_output(raw)
    assert result[0][0] == "src/lib/util.py"


def test_header_with_trailing_spaces():
    """Header like '=== FILE: calc.py ===' with trailing space is handled."""
    raw = "=== FILE: calc.py ===  \ndef add(a, b): return a + b\n"
    result = _parse_multi_file_output(raw)
    assert len(result) == 1
    assert result[0][0] == "calc.py"
