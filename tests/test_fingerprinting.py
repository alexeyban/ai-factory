"""
Tests for Phase 9 — Solution Fingerprinting.

Verifies compute_code_hash normalisation and EpisodicMemory duplicate detection.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.episodic import compute_code_hash, EpisodicMemory


# ---------------------------------------------------------------------------
# compute_code_hash — pure function tests
# ---------------------------------------------------------------------------

def test_same_code_same_hash():
    code = "def foo():\n    return 42\n"
    assert compute_code_hash(code) == compute_code_hash(code)


def test_whitespace_normalization():
    """Extra blank lines and trailing spaces must not change the hash."""
    code_a = "def foo():\n    return 42\n"
    code_b = "def foo():\n\n    return 42\n\n"
    assert compute_code_hash(code_a) == compute_code_hash(code_b)


def test_different_code_different_hash():
    code_a = "def foo():\n    return 42\n"
    code_b = "def foo():\n    return 99\n"
    assert compute_code_hash(code_a) != compute_code_hash(code_b)


def test_comment_normalization():
    """Comments stripped by AST round-trip must not affect the hash."""
    code_a = "def foo():\n    return 42\n"
    code_b = "def foo():\n    # inline comment\n    return 42\n"
    assert compute_code_hash(code_a) == compute_code_hash(code_b)


def test_syntax_error_fallback():
    """Invalid Python falls back to stripped raw text — must not raise."""
    bad_code = "this is not valid python !!!"
    h = compute_code_hash(bad_code)
    assert isinstance(h, str) and len(h) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# EpisodicMemory.check_solution_fingerprint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_detection():
    """check_solution_fingerprint returns True when the hash is already stored."""
    mock_db = MagicMock()
    mock_db.fetchval = AsyncMock(return_value=1)  # hash already exists

    mem = EpisodicMemory(db=mock_db)
    result = await mem.check_solution_fingerprint("abc123", "T001")

    assert result is True
    mock_db.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_duplicate_returns_false():
    mock_db = MagicMock()
    mock_db.fetchval = AsyncMock(return_value=0)

    mem = EpisodicMemory(db=mock_db)
    result = await mem.check_solution_fingerprint("newcode", "T002")

    assert result is False
