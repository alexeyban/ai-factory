"""
pytest configuration — stub heavy optional dependencies so unit tests
can import memory.* modules without asyncpg / qdrant-client installed.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock


def _make_asyncpg_stub() -> types.ModuleType:
    mod = types.ModuleType("asyncpg")
    mod.create_pool = AsyncMock()  # type: ignore[attr-defined]
    mod.connect = AsyncMock()  # type: ignore[attr-defined]
    return mod


def _make_qdrant_stub() -> None:
    """Register qdrant_client and its sub-modules as MagicMock stubs."""
    qdrant_client = types.ModuleType("qdrant_client")
    qdrant_client.QdrantClient = MagicMock  # type: ignore[attr-defined]
    sys.modules.setdefault("qdrant_client", qdrant_client)

    models_mod = types.ModuleType("qdrant_client.models")
    for cls_name in ("Distance", "VectorParams", "PointStruct"):
        setattr(models_mod, cls_name, MagicMock())
    sys.modules.setdefault("qdrant_client.models", models_mod)

    http_mod = types.ModuleType("qdrant_client.http")
    http_models = types.ModuleType("qdrant_client.http.models")
    for cls_name in ("Distance", "VectorParams", "PointStruct"):
        setattr(http_models, cls_name, MagicMock())
    sys.modules.setdefault("qdrant_client.http", http_mod)
    sys.modules.setdefault("qdrant_client.http.models", http_models)


# Install stubs once — before any test module imports memory.*
sys.modules.setdefault("asyncpg", _make_asyncpg_stub())
_make_qdrant_stub()
