"""Persistent Chroma storage plus a tiny in-memory test implementation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .splitter import MarketChunk


def _metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    result: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if value is None:
            result[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            result[key] = value
        else:
            result[key] = str(value)
    return result


class ChromaMarketVectorStore:
    def __init__(self, persist_dir: Path, collection_name: str = "market_rag"):
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "chromadb is required for the persistent market vector store"
            ) from exc
        persist_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(persist_dir), settings=Settings(anonymized_telemetry=False)
        )
        self.collection_name = collection_name
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine", "domain": "healthcare_ai_market"},
        )

    def rebuild(self) -> None:
        try:
            self.client.delete_collection(self.collection_name)
        except Exception:  # noqa: BLE001, S110 - a missing collection is valid on rebuild
            pass
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine", "domain": "healthcare_ai_market"},
        )

    def upsert(
        self, chunks: Sequence[MarketChunk], embeddings: Sequence[Sequence[float]]
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        if not chunks:
            return
        self.collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            metadatas=[_metadata(chunk.metadata) for chunk in chunks],
            embeddings=[list(vector) for vector in embeddings],
        )

    def query(self, embedding: Sequence[float], top_k: int = 5) -> list[dict[str, Any]]:
        result = self.collection.query(
            query_embeddings=[list(embedding)],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            {
                "chunk_id": chunk_id,
                "text": document,
                "metadata": metadata or {},
                "score": max(0.0, 1.0 - float(distance)),
            }
            for chunk_id, document, metadata, distance in zip(
                ids, documents, metadatas, distances
            )
        ]

    def count(self) -> int:
        return self.collection.count()


class InMemoryVectorStore:
    """Small deterministic store for unit tests and offline wiring checks."""

    def __init__(self):
        self.rows: dict[str, tuple[MarketChunk, list[float]]] = {}

    def rebuild(self) -> None:
        self.rows.clear()

    def upsert(
        self, chunks: Sequence[MarketChunk], embeddings: Sequence[Sequence[float]]
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        for chunk, embedding in zip(chunks, embeddings):
            self.rows[chunk.chunk_id] = (chunk, list(embedding))

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        ln = math.sqrt(sum(a * a for a in left))
        rn = math.sqrt(sum(b * b for b in right))
        return dot / (ln * rn) if ln and rn else 0.0

    def query(self, embedding: Sequence[float], top_k: int = 5) -> list[dict[str, Any]]:
        ranked = sorted(
            (
                (self._cosine(embedding, vector), chunk)
                for chunk, vector in self.rows.values()
            ),
            key=lambda item: item[0],
            reverse=True,
        )[:top_k]
        return [
            {
                "chunk_id": chunk.chunk_id,
                "text": chunk.text,
                "metadata": chunk.metadata,
                "score": score,
            }
            for score, chunk in ranked
        ]

    def count(self) -> int:
        return len(self.rows)
