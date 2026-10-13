"""Market dataset/index and RAG preparation settings, independent of the worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MarketConfig:
    schema_version: str = "2.0"
    criteria_version: str = "proposed-2.0"
    dataset_dir: Path = PROJECT_ROOT / "data" / "market_rag"
    vectorstore_dir: Path = PROJECT_ROOT / "data" / "vectorstore" / "market"
    collection_name: str = "market_rag"
    embedding_model: str = "BAAI/bge-m3"
    chunk_size_tokens: int = 700
    chunk_overlap_tokens: int = 100
    retrieval_top_k: int = 5
    max_retrieved_chunks_per_dimension: int = 10
    max_chars_per_chunk: int = 4000
    max_chars_per_dimension: int = 16000
    max_context_chars: int = 6000

    def __post_init__(self) -> None:
        for name in (
            "retrieval_top_k", "max_retrieved_chunks_per_dimension",
            "max_chars_per_chunk", "max_chars_per_dimension", "max_context_chars",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_env(cls) -> MarketConfig:
        def path(name: str, default: Path) -> Path:
            value = Path(os.getenv(name, str(default))).expanduser()
            return value if value.is_absolute() else PROJECT_ROOT / value

        return cls(
            dataset_dir=path("MARKET_RAG_DATA_DIR", PROJECT_ROOT / "data" / "market_rag"),
            vectorstore_dir=path("MARKET_RAG_VECTORSTORE_DIR", PROJECT_ROOT / "data" / "vectorstore" / "market"),
            collection_name=os.getenv("MARKET_RAG_COLLECTION", "market_rag"),
            embedding_model=os.getenv("MARKET_EMBEDDING_MODEL", "BAAI/bge-m3"),
            chunk_size_tokens=int(os.getenv("MARKET_RAG_CHUNK_SIZE", "700")),
            chunk_overlap_tokens=int(os.getenv("MARKET_RAG_CHUNK_OVERLAP", "100")),
            retrieval_top_k=int(os.getenv("MARKET_RAG_TOP_K", "5")),
            max_retrieved_chunks_per_dimension=int(os.getenv("MARKET_RAG_MAX_CHUNKS", "10")),
            max_chars_per_chunk=int(os.getenv("MARKET_RAG_MAX_CHUNK_CHARS", "4000")),
            max_chars_per_dimension=int(os.getenv("MARKET_RAG_MAX_DIMENSION_CHARS", "16000")),
            max_context_chars=int(os.getenv("MARKET_RAG_MAX_CONTEXT_CHARS", "6000")),
        )
