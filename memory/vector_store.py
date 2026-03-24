"""
Vector memory backed by Qdrant for skill and episode similarity search.

Two collections are maintained:
  - "skills"   — embeddings of extracted skill descriptions
  - "episodes" — embeddings of task descriptions from past workflow runs

Embedding is obtained via the shared LLM adapter (text-embedding models).
Falls back to zero-vector when an embedding call fails, so the system
degrades gracefully rather than crashing.
"""
from __future__ import annotations

import logging
import os
from typing import Any

LOGGER = logging.getLogger(__name__)

SKILLS_COLLECTION = "skills"
EPISODES_COLLECTION = "episodes"
VECTOR_DIM = 1536  # default; matches text-embedding-3-small


def _default_qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", "http://localhost:6333")


class VectorMemory:
    """
    Thin async wrapper around the Qdrant client.

    The Qdrant Python client is synchronous; all methods here are declared
    `async` for consistency with the rest of the memory layer. The actual
    network calls are blocking — this is acceptable because:
      1. Qdrant operations are fast (sub-millisecond on LAN)
      2. They run inside Temporal activities, not workflow coroutines
    """

    def __init__(
        self,
        qdrant_url: str | None = None,
        vector_dim: int = VECTOR_DIM,
    ) -> None:
        from qdrant_client import QdrantClient  # lazy import
        self._url = qdrant_url or _default_qdrant_url()
        self._dim = vector_dim
        self._client = QdrantClient(url=self._url)

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def init_collections(self) -> None:
        """Create skill and episode collections if they do not exist."""
        from qdrant_client.models import Distance, VectorParams

        for name in [SKILLS_COLLECTION, EPISODES_COLLECTION]:
            if not self._collection_exists(name):
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(
                        size=self._dim,
                        distance=Distance.COSINE,
                    ),
                )
                LOGGER.info(f"Created Qdrant collection: {name}")
            else:
                LOGGER.debug(f"Qdrant collection already exists: {name}")

    def _collection_exists(self, name: str) -> bool:
        try:
            self._client.get_collection(name)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    async def upsert_skill(
        self,
        skill_id: str,
        embedding: list[float],
        metadata: dict,
    ) -> None:
        from qdrant_client.models import PointStruct

        self._client.upsert(
            collection_name=SKILLS_COLLECTION,
            points=[PointStruct(id=skill_id, vector=embedding, payload=metadata)],
        )

    async def search_similar_skills(
        self,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict]:
        results = self._client.search(
            collection_name=SKILLS_COLLECTION,
            query_vector=embedding,
            limit=top_k,
        )
        return [{"id": r.id, "score": r.score, **r.payload} for r in results]

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    async def upsert_episode(
        self,
        episode_id: str,
        task_embedding: list[float],
        metadata: dict,
    ) -> None:
        from qdrant_client.models import PointStruct

        # Qdrant point IDs must be unsigned integers or UUIDs.
        # Use a stable integer derived from the episode_id string.
        point_id = _stable_id(episode_id)
        self._client.upsert(
            collection_name=EPISODES_COLLECTION,
            points=[PointStruct(id=point_id, vector=task_embedding,
                                payload={"episode_id": episode_id, **metadata})],
        )

    async def search_similar_episodes(
        self,
        task_embedding: list[float],
        top_k: int = 3,
    ) -> list[dict]:
        results = self._client.search(
            collection_name=EPISODES_COLLECTION,
            query_vector=task_embedding,
            limit=top_k,
        )
        return [{"id": r.id, "score": r.score, **r.payload} for r in results]

    # ------------------------------------------------------------------
    # Embedding helper
    # ------------------------------------------------------------------

    @staticmethod
    async def embed_text(text: str) -> list[float]:
        """
        Return an embedding vector for `text` using the shared LLM adapter.

        Falls back to a zero vector on failure so the caller is never blocked.
        The zero vector means similarity search will return arbitrary results —
        acceptable as a degraded-mode fallback.
        """
        try:
            from shared.llm import get_embedding
            return await get_embedding(text)
        except Exception as exc:
            LOGGER.warning(f"embed_text failed, using zero vector: {exc}")
            return [0.0] * VECTOR_DIM


def _stable_id(s: str) -> int:
    """Convert a string to a stable positive integer suitable as a Qdrant point ID."""
    import hashlib
    digest = hashlib.sha256(s.encode()).digest()
    # Use first 8 bytes as a big-endian uint64, mask to positive int63
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
