"""
Skill data model for the AI Factory self-learning loop.

A Skill is a reusable code pattern extracted from a successful dev solution.
Skills are stored in PostgreSQL (metadata), Qdrant (embedding), and as
plain Python files under skills/<id>.py.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Skill:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    code_path: str = ""             # relative path: skills/<id>.py
    embedding: list[float] = field(default_factory=list)
    success_rate: float = 0.0       # 0.0 – 1.0
    use_count: int = 0
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime | None = None
    last_optimized_at: datetime | None = None
    is_active: bool = True

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (suitable for JSON / Qdrant payload)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "code_path": self.code_path,
            "success_rate": self.success_rate,
            "use_count": self.use_count,
            "tags": list(self.tags),
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "is_active": self.is_active,
            # Embeddings are large; omit from generic dict to avoid bloating
            # payloads — store them separately in Qdrant.
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        """Deserialise from a plain dict."""

        def _parse_dt(val: str | None) -> datetime | None:
            if not val:
                return None
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                return None

        created_raw = data.get("created_at")
        created = (
            datetime.fromisoformat(created_raw)
            if created_raw
            else datetime.now(timezone.utc)
        )

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data.get("name", ""),
            description=data.get("description", ""),
            code_path=data.get("code_path", ""),
            embedding=data.get("embedding", []),
            success_rate=float(data.get("success_rate", 0.0)),
            use_count=int(data.get("use_count", 0)),
            tags=list(data.get("tags", [])),
            created_at=created,
            last_used_at=_parse_dt(data.get("last_used_at")),
            is_active=bool(data.get("is_active", True)),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def embed_text(self) -> str:
        """Text used to generate the embedding (description + tags)."""
        parts = [self.description] + self.tags
        return " ".join(p for p in parts if p)

    def __repr__(self) -> str:  # noqa: D105
        return (
            f"Skill(id={self.id!r}, name={self.name!r}, "
            f"success_rate={self.success_rate:.2f}, tags={self.tags})"
        )
