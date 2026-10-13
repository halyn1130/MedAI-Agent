"""Offline market dataset/index preparation settings, independent of the worker."""

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

    @classmethod
    def from_env(cls) -> MarketConfig:
        return cls(
            dataset_dir=Path(
                os.getenv("MARKET_RAG_DATA_DIR", PROJECT_ROOT / "data" / "market_rag")
            ),
            vectorstore_dir=Path(
                os.getenv(
                    "MARKET_RAG_VECTORSTORE_DIR",
                    PROJECT_ROOT / "data" / "vectorstore" / "market",
                )
            ),
            collection_name=os.getenv("MARKET_RAG_COLLECTION", "market_rag"),
            embedding_model=os.getenv("MARKET_EMBEDDING_MODEL", "BAAI/bge-m3"),
            chunk_size_tokens=int(os.getenv("MARKET_RAG_CHUNK_SIZE", "700")),
            chunk_overlap_tokens=int(os.getenv("MARKET_RAG_CHUNK_OVERLAP", "100")),
        )
