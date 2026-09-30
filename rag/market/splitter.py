"""Configurable page-preserving text chunker."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .loader import PageDocument

TOKEN_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class MarketChunk:
    chunk_id: str
    text: str
    metadata: dict[str, Any]


class TokenChunker:
    """A deterministic whitespace-token baseline.

    The size is an approximate token count.  It is intentionally configurable;
    a future model-tokenizer splitter can replace this class without changing
    the loader, vector store, or retriever interfaces.
    """

    def __init__(self, chunk_size: int = 700, overlap: int = 100):
        if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
            raise ValueError(
                "chunk_size must be positive and overlap must be smaller than chunk_size"
            )
        self.chunk_size = chunk_size
        self.overlap = overlap

    def split_page(self, page: PageDocument) -> list[MarketChunk]:
        tokens = TOKEN_RE.findall(page.text)
        if not tokens:
            return []
        chunks: list[MarketChunk] = []
        step = self.chunk_size - self.overlap
        for index, start in enumerate(range(0, len(tokens), step)):
            window = tokens[start : start + self.chunk_size]
            if not window:
                break
            text = " ".join(window)
            raw_id = (
                f"{page.metadata['document_id']}|{page.metadata['page']}|{index}|{text}"
            )
            chunk_id = (
                "market-chunk-"
                + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
            )
            chunks.append(
                MarketChunk(
                    chunk_id=chunk_id,
                    text=text,
                    metadata={
                        **page.metadata,
                        "chunk_index": index,
                        "token_start": start,
                        "token_count": len(window),
                    },
                )
            )
            if start + self.chunk_size >= len(tokens):
                break
        return chunks

    def split_documents(self, pages: Iterable[PageDocument]) -> list[MarketChunk]:
        return [chunk for page in pages for chunk in self.split_page(page)]
