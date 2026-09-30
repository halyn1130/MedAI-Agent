"""Embedding adapters.  BAAI/bge-m3 is loaded lazily."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class BgeM3Embeddings:
    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        device: str | None = None,
        batch_size: int = 16,
    ):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model = None
        # LangGraph fans four analysis nodes out concurrently. SentenceTransformer
        # model construction and encode calls are guarded so one shared retriever
        # does not load four copies of bge-m3 or invoke the backend concurrently.
        self._lock = threading.RLock()

    @property
    def model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as exc:  # pragma: no cover
                        raise RuntimeError(
                            "sentence-transformers is required for BAAI/bge-m3 embeddings"
                        ) from exc
                    self._model = SentenceTransformer(
                        self.model_name, device=self.device
                    )
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        with self._lock:
            model = self.model
            encode = getattr(model, "encode_document", model.encode)
            vectors = encode(
                values,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            model = self.model
            encode = getattr(model, "encode_query", model.encode)
            vector = encode(
                text,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return vector.tolist()
