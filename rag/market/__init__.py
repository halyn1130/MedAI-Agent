"""Dense retrieval pipeline for market and business evidence."""

from .embeddings import BgeM3Embeddings, EmbeddingProvider
from .loader import DatasetStats, PageDocument, count_dataset_pages, load_pdf_pages
from .retriever import DenseRetriever, RetrievedChunk, Retriever
from .splitter import MarketChunk, TokenChunker
from .vectorstore import ChromaMarketVectorStore, InMemoryVectorStore

__all__ = [
    "BgeM3Embeddings",
    "ChromaMarketVectorStore",
    "DatasetStats",
    "DenseRetriever",
    "EmbeddingProvider",
    "InMemoryVectorStore",
    "MarketChunk",
    "PageDocument",
    "RetrievedChunk",
    "Retriever",
    "TokenChunker",
    "count_dataset_pages",
    "load_pdf_pages",
]
