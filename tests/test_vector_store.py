"""
Tests for memory/vector_store.py

Qdrant client is fully mocked — no real Qdrant server required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memory.vector_store import VectorMemory, _stable_id, VECTOR_DIM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vector_memory() -> tuple[VectorMemory, MagicMock]:
    """Return a VectorMemory with a mocked Qdrant client."""
    with patch("memory.vector_store.QdrantClient.__init__", return_value=None):
        vm = VectorMemory.__new__(VectorMemory)
        vm._url = "http://localhost:6333"
        vm._dim = VECTOR_DIM
        vm._client = MagicMock()
        return vm, vm._client


def _fake_embedding(value: float = 0.5) -> list[float]:
    return [value] * VECTOR_DIM


# ---------------------------------------------------------------------------
# _stable_id
# ---------------------------------------------------------------------------

def test_stable_id_deterministic():
    assert _stable_id("ep_abc") == _stable_id("ep_abc")


def test_stable_id_different_inputs():
    assert _stable_id("ep_abc") != _stable_id("ep_xyz")


def test_stable_id_positive():
    assert _stable_id("any_string") >= 0


# ---------------------------------------------------------------------------
# init_collections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_collections_creates_if_missing():
    vm, client = _make_vector_memory()
    # Simulate both collections missing
    client.get_collection.side_effect = Exception("not found")

    await vm.init_collections()

    assert client.create_collection.call_count == 2


@pytest.mark.asyncio
async def test_init_collections_skips_existing():
    vm, client = _make_vector_memory()
    # Simulate both collections already existing
    client.get_collection.return_value = MagicMock()

    await vm.init_collections()

    client.create_collection.assert_not_called()


# ---------------------------------------------------------------------------
# upsert_skill / search_similar_skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_skill_calls_client():
    vm, client = _make_vector_memory()
    embedding = _fake_embedding()
    await vm.upsert_skill("skill-001", embedding, {"name": "sort_list"})
    client.upsert.assert_called_once()
    call_kwargs = client.upsert.call_args[1]
    assert call_kwargs["collection_name"] == "skills"


@pytest.mark.asyncio
async def test_search_similar_skills_returns_results():
    vm, client = _make_vector_memory()
    hit = MagicMock()
    hit.id = "skill-001"
    hit.score = 0.97
    hit.payload = {"name": "sort_list", "tags": ["algorithm"]}
    client.search.return_value = [hit]

    results = await vm.search_similar_skills(_fake_embedding(), top_k=3)

    assert len(results) == 1
    assert results[0]["id"] == "skill-001"
    assert results[0]["score"] == pytest.approx(0.97)
    assert results[0]["name"] == "sort_list"


# ---------------------------------------------------------------------------
# upsert_episode / search_similar_episodes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_episode_calls_client():
    vm, client = _make_vector_memory()
    await vm.upsert_episode("ep_001", _fake_embedding(), {"task_id": "T001"})
    client.upsert.assert_called_once()
    call_kwargs = client.upsert.call_args[1]
    assert call_kwargs["collection_name"] == "episodes"


@pytest.mark.asyncio
async def test_search_similar_episodes_returns_results():
    vm, client = _make_vector_memory()
    hit = MagicMock()
    hit.id = 12345
    hit.score = 0.88
    hit.payload = {"episode_id": "ep_001", "task_id": "T001"}
    client.search.return_value = [hit]

    results = await vm.search_similar_episodes(_fake_embedding(), top_k=3)

    assert len(results) == 1
    assert results[0]["score"] == pytest.approx(0.88)
    assert results[0]["task_id"] == "T001"


# ---------------------------------------------------------------------------
# embed_text fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_text_falls_back_on_error():
    with patch("memory.vector_store.VectorMemory.embed_text") as mock_embed:
        mock_embed.return_value = [0.0] * VECTOR_DIM
        result = await VectorMemory.embed_text("some task description")
        assert len(result) == VECTOR_DIM
