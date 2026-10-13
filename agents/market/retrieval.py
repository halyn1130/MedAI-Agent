"""Select bounded context from the worker's fixed local PDF index."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .agent import _value
from .config import PROJECT_ROOT, MarketConfig
from .contract import Dataset, WorkerInput, parse_input, provided_dataset, record_text
from .schema import DIMENSIONS

if TYPE_CHECKING:
    from rag.market.retriever import Retriever

_DEFAULT_PREPARER = None
_DEFAULT_PREPARER_LOCK = threading.Lock()


class MarketRagPreparationError(RuntimeError):
    """RAG failed; the caller must not replace retrieval with the complete corpus."""


def market_queries(request: WorkerInput) -> dict[str, list[str]]:
    """Use the supplied product/customer context, without an extra LLM/search call."""
    profile = request.company_profile

    def value(field: str) -> str:
        raw = _value(profile, field)
        return ", ".join(map(str, raw)) if isinstance(raw, list) else str(raw or "")

    product = f"{value('product_type')} {value('intended_use')}".strip() or "의료 인공지능"
    customer = f"{value('target_customer')} {value('buyer')}".strip()
    geography = value("geography") or "global"
    year = int(request.as_of[:4])
    queries = {
        "size_growth": [
            f"{product} {geography} 시장 규모 성장률 전망 CAGR {year}-{year + 5}",
            f"{product} {geography} market size growth forecast CAGR",
        ],
        "demand": [
            f"{product} {customer} {value('core_problem')} 미충족 수요 도입 의향 예산",
            f"{product} {customer} unmet need demand willingness to pay",
        ],
        "commercialization": [
            f"{product} {customer} 도입 상용화 인프라 업무흐름 통합 조달 장벽",
            f"{product} {customer} implementation workflow integration procurement barriers",
        ],
        "monetization": [
            f"{product} {customer} {value('business_model')} 수익모델 과금 가격 지불 ROI",
            f"{product} pricing subscription license payment pathway economics ROI",
        ],
    }
    review = request.review_request or {}
    extras = [
        q.get("text", "") if isinstance(q, dict) else str(q)
        for q in review.get("questions", [])[:2]
    ]
    for dimension in review.get("dimensions") or DIMENSIONS:
        if dimension in queries:
            queries[dimension] = list(dict.fromkeys(
                [f"{product} {q[:300]}" for q in extras if q] + queries[dimension]
            ))
    return queries


def _is_company_record(raw: dict, company_id: str) -> bool:
    """This company's own supplied fact (company_id), or a re-collected record
    (agents/data_collect/recollect.py) marked with company_ids and the market tag.
    The tag check keeps other workers' company records out when a whole dataset is forwarded."""
    if raw.get("company_id") == company_id:
        return True
    return company_id in (raw.get("company_ids") or []) and "market" in (raw.get("tags") or [])


class MarketRagPreparer:
    def __init__(self, retriever: Retriever, *, config: MarketConfig | None = None):
        self.retriever = retriever
        self.config = config or MarketConfig()

    def prepare(self, worker_input: dict | WorkerInput) -> dict:
        request = parse_input(worker_input, "market")
        supplied_sources, supplied_records, _ = provided_dataset(request, "market")
        declared_ids = {s["source_id"] for s in request.dataset.sources}
        declared = {s["source_id"]: s for s in supplied_sources}
        sources, records = {}, []

        # Only explicitly supplied company facts supplement the company profile.
        # Industry PDF pages are replaced by RAG results, never forwarded wholesale.
        remaining = self.config.max_context_chars
        for raw in supplied_records:
            if raw.get("dimension") != "context" and not _is_company_record(raw, request.company_id):
                continue
            if raw.get("dimension") not in (None, "context"):
                continue
            text = record_text(raw)[:min(remaining, self.config.max_chars_per_chunk)]
            if not text:
                continue
            records.append({
                "record_id": raw.get("record_id"), "source_ids": raw["source_ids"],
                "company_id": request.company_id, "agent": "market",
                "dimension": "context", "text": text,
                "page": raw.get("page"), "locator": raw.get("locator"),
            })
            for sid in raw["source_ids"]:
                sources[sid] = declared[sid]
            remaining -= len(text)
            if not remaining:
                break

        for dimension, queries in market_queries(request).items():
            hits = {}
            for query in queries:
                try:
                    found = self.retriever.retrieve(query, top_k=self.config.retrieval_top_k)
                except Exception as exc:
                    raise MarketRagPreparationError(
                        f"market RAG retrieval failed for {dimension}; no full-PDF fallback"
                    ) from exc
                for chunk in found:
                    meta = chunk.metadata
                    sid = meta.get("document_id")
                    if not sid or (declared_ids and sid not in declared):
                        continue
                    if meta.get("company_id") not in (None, "", request.company_id):
                        continue
                    if meta.get("agent") not in (None, "", "market"):
                        continue
                    if any(str(meta.get(k) or "")[:10] > request.as_of for k in ("published_at", "event_date")):
                        continue
                    if not chunk.text.strip() or not meta.get("page"):
                        continue
                    old = hits.get(chunk.chunk_id)
                    if old is None or chunk.score > old.score:
                        hits[chunk.chunk_id] = chunk

            remaining = self.config.max_chars_per_dimension
            ranked = sorted(hits.values(), key=lambda c: (-c.score, c.chunk_id))
            for chunk in ranked[:self.config.max_retrieved_chunks_per_dimension]:
                meta = chunk.metadata
                text = chunk.text[:min(remaining, self.config.max_chars_per_chunk)]
                if not text:
                    break
                sid = meta["document_id"]
                source = {
                    "source_id": sid, "document_id": sid,
                    "title": meta.get("title") or sid,
                    "url": meta.get("source_url") or meta.get("path") or sid,
                    "publisher": meta.get("publisher") or "unknown",
                    "published_at": meta.get("published_at") or None,
                    "checked_at": meta.get("checked_at") or None,
                    "source_type": meta.get("source_type") or "research",
                    "region": meta.get("region"), "segment": meta.get("segment"),
                    "path": meta.get("path"),
                }
                for key in source.keys() - {"source_id", "document_id"}:
                    if key in declared.get(sid, {}):
                        source[key] = declared[sid][key]
                sources[sid] = source
                records.append({
                    "record_id": f"{chunk.chunk_id}:{dimension}",
                    "chunk_id": chunk.chunk_id, "agent": "market",
                    "company_id": request.company_id, "dimension": dimension,
                    "source_id": sid, "document_id": sid, "text": text,
                    "page": meta["page"],
                    "locator": f"{meta.get('path') or sid} page {meta['page']} chunk {meta.get('chunk_index', 0)}",
                    "geography": source["region"], "segment": source["segment"],
                    "published_at": source["published_at"],
                    "retrieval_score": chunk.score,
                })
                remaining -= len(text)
                if not remaining:
                    break
        prepared = request.model_dump(mode="json")
        prepared["dataset"] = Dataset(
            version=request.dataset.version, sources=list(sources.values()), records=records
        ).model_dump(mode="json")
        return prepared


def default_market_preparer() -> MarketRagPreparer:
    global _DEFAULT_PREPARER
    if _DEFAULT_PREPARER is None:
        with _DEFAULT_PREPARER_LOCK:
            if _DEFAULT_PREPARER is None:
                from dotenv import load_dotenv
                from rag.market.embeddings import BgeM3Embeddings
                from rag.market.retriever import DenseRetriever
                from rag.market.vectorstore import ChromaMarketVectorStore

                load_dotenv(PROJECT_ROOT / ".env")
                config = MarketConfig.from_env()
                try:
                    store = ChromaMarketVectorStore(
                        config.vectorstore_dir, config.collection_name, create_if_missing=False
                    )
                    if not store.count():
                        raise ValueError("market RAG collection is empty")
                except Exception as exc:
                    raise MarketRagPreparationError(
                        "market RAG index unavailable; run python -m rag.market.build_index --rebuild"
                    ) from exc
                _DEFAULT_PREPARER = MarketRagPreparer(
                    DenseRetriever(
                        BgeM3Embeddings(config.embedding_model, local_files_only=True), store
                    ), config=config
                )
    return _DEFAULT_PREPARER


def prepare_market_input(
    worker_input: dict | WorkerInput, *, preparer: MarketRagPreparer | None = None
) -> dict:
    return (preparer or default_market_preparer()).prepare(worker_input)
