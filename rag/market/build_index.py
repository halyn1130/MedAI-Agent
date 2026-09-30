"""Build the local persistent dense index from ``data/market_rag``.

Run from the project root:

    python -m rag.market.build_index --rebuild
"""

from __future__ import annotations

import argparse

from agents.market.config import MarketConfig

from .embeddings import BgeM3Embeddings
from .loader import count_dataset_pages, load_pdf_pages, require_page_budget
from .splitter import TokenChunker
from .vectorstore import ChromaMarketVectorStore


def build_index(
    *, rebuild: bool = False, allow_over_limit: bool = False, max_pages: int = 200
) -> dict:
    config = MarketConfig.from_env()
    stats = count_dataset_pages(config.dataset_dir)
    if stats.pdf_files == 0:
        raise ValueError(f"no PDF files found under {config.dataset_dir}")
    require_page_budget(stats, max_pages, allow_over_limit=allow_over_limit)
    pages, parsed_stats = load_pdf_pages(config.dataset_dir)
    splitter = TokenChunker(config.chunk_size_tokens, config.chunk_overlap_tokens)
    chunks = splitter.split_documents(pages)
    if not chunks:
        raise ValueError("the selected PDF pages did not yield any text chunks")
    embeddings = BgeM3Embeddings(config.embedding_model)
    store = ChromaMarketVectorStore(config.vectorstore_dir, config.collection_name)
    if rebuild:
        store.rebuild()
    batch_size = 64
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = embeddings.embed_documents([chunk.text for chunk in batch])
        store.upsert(batch, vectors)
    return {
        "pdf_files": parsed_stats.pdf_files,
        "physical_pages": parsed_stats.physical_pages,
        "indexed_pages": parsed_stats.indexed_pages,
        "empty_text_pages": parsed_stats.empty_text_pages,
        "chunks": len(chunks),
        "stored_chunks": store.count(),
        "vectorstore_dir": str(config.vectorstore_dir),
        "collection": config.collection_name,
        "embedding_model": config.embedding_model,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild", action="store_true", help="replace the existing market collection"
    )
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--allow-over-limit", action="store_true")
    args = parser.parse_args()
    result = build_index(
        rebuild=args.rebuild,
        allow_over_limit=args.allow_over_limit,
        max_pages=args.max_pages,
    )
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
