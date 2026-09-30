from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pypdf import PdfWriter

from rag.market.embeddings import BgeM3Embeddings
from rag.market.loader import (
    PageDocument,
    count_dataset_pages,
    load_pdf_pages,
    parse_page_spec,
    require_page_budget,
)
from rag.market.retriever import DenseRetriever
from rag.market.splitter import TokenChunker
from rag.market.vectorstore import InMemoryVectorStore


class TinyEmbeddings:
    def embed_documents(self, texts):
        return [[float("market" in text.lower()), float(len(text))] for text in texts]

    def embed_query(self, text):
        return [float("market" in text.lower()), float(len(text))]


def test_page_spec_and_budget(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    with (reports / "sample.pdf").open("wb") as stream:
        writer.write(stream)
    (tmp_path / "metadata.csv").write_text(
        "path,title,exclude_pages\nreports/sample.pdf,Sample report,2\n",
        encoding="utf-8",
    )

    stats = count_dataset_pages(tmp_path)

    assert parse_page_spec("1-3,7") == {1, 2, 3, 7}
    assert stats.physical_pages == 3
    assert stats.indexed_pages == 2
    require_page_budget(stats, 2)
    with pytest.raises(ValueError):
        require_page_budget(stats, 1)


def test_loader_counts_empty_pages_but_keeps_selected_page_budget(tmp_path: Path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with (tmp_path / "blank.pdf").open("wb") as stream:
        writer.write(stream)

    pages, stats = load_pdf_pages(tmp_path)

    assert pages == []
    assert stats.indexed_pages == 1
    assert stats.empty_text_pages == 1


def test_chunk_and_dense_retrieval_preserve_page_metadata():
    page = PageDocument(
        text="market demand evidence for healthcare AI buyers " * 12,
        metadata={
            "document_id": "doc-1",
            "title": "Market report",
            "publisher": "Institute",
            "published_at": "2025-01-01",
            "region": "KR",
            "segment": "healthcare_ai",
            "source_type": "industry_report",
            "source_url": "https://example.org/report",
            "path": "reports/report.pdf",
            "page": 11,
        },
    )
    chunks = TokenChunker(chunk_size=20, overlap=5).split_documents([page])
    embeddings = TinyEmbeddings()
    store = InMemoryVectorStore()
    store.upsert(chunks, embeddings.embed_documents([item.text for item in chunks]))

    results = DenseRetriever(embeddings, store).retrieve("market demand", top_k=2)

    assert results
    assert all(item.metadata["page"] == 11 for item in results)
    assert all(item.metadata["document_id"] == "doc-1" for item in results)


def test_chunker_validates_overlap():
    with pytest.raises(ValueError):
        TokenChunker(chunk_size=100, overlap=100)


def test_bge_model_is_loaded_once_across_parallel_graph_nodes(monkeypatch):
    import sentence_transformers

    loads = 0

    class FakeModel:
        def __init__(self, model_name, device=None):
            nonlocal loads
            time.sleep(0.01)
            loads += 1

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", FakeModel)
    embeddings = BgeM3Embeddings()

    with ThreadPoolExecutor(max_workers=4) as pool:
        models = list(pool.map(lambda _: embeddings.model, range(4)))

    assert loads == 1
    assert len({id(model) for model in models}) == 1
