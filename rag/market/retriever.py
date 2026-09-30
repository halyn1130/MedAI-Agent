"""Dense retrieval interface with extension hooks for hybrid search/reranking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .embeddings import EmbeddingProvider


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    score: float


class Retriever(Protocol):
    def retrieve(self, query: str, *, top_k: int = 5) -> list[RetrievedChunk]: ...


class Reranker(Protocol):
    def rerank(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]: ...


class DenseRetriever:
    def __init__(
        self,
        embeddings: EmbeddingProvider,
        vectorstore,
        *,
        reranker: Reranker | None = None,
    ):
        self.embeddings = embeddings
        self.vectorstore = vectorstore
        self.reranker = reranker

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RetrievedChunk]:
        vector = self.embeddings.embed_query(query)
        chunks = [
            RetrievedChunk(**row) for row in self.vectorstore.query(vector, top_k=top_k)
        ]
        if self.reranker is not None:
            chunks = self.reranker.rerank(query, chunks)
        return chunks[:top_k]


class EmptyRetriever:
    def retrieve(self, query: str, *, top_k: int = 5) -> list[RetrievedChunk]:
        return []
