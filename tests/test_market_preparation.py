from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace

import pytest

from agents.market import MarketNode, WorkerResult
from agents.market.config import MarketConfig
from agents.market.contract import WorkerInput
from agents.market.retrieval import (
    MarketRagPreparationError,
    MarketRagPreparer,
    market_queries,
)
from agents.market.schema import DIMENSIONS
from rag.market.retriever import RetrievedChunk
from test_market_agent import FakeReasoner, complete_profile, worker_input


def chunk(identity="chunk-7", *, text="Selected industry evidence.", score=0.9, **metadata):
    return RetrievedChunk(identity, text, {
        "document_id": "report-1", "page": 7, "chunk_index": 0,
        "path": "reports/report.pdf", "published_at": "2025-06-01",
        "title": "Healthcare AI Adoption Report", "publisher": "Research Institute",
        "source_url": "https://research.example/report", "region": "KR",
        "segment": "clinical decision support software", **metadata,
    }, score)


class FakeRetriever:
    def __init__(self, chunks=None):
        self.chunks = [chunk()] if chunks is None else chunks
        self.calls = []

    def retrieve(self, query, *, top_k):
        self.calls.append((query, top_k))
        return self.chunks


class CapturingReasoner(FakeReasoner):
    def __init__(self):
        super().__init__()
        self.inputs = {}
        self.supplement_calls = []

    def supplement(self, *args):
        self.supplement_calls.append(copy.deepcopy(args))
        return super().supplement(*args)

    def analyze_dimension(self, dimension, scope, profile, supplemental, evidence, rubric, review_request=None):
        self.inputs[dimension] = copy.deepcopy((evidence, review_request))
        return super().analyze_dimension(
            dimension, scope, profile, supplemental, evidence, rubric, review_request
        )


def test_preparation_replaces_entire_pdf_input_and_preserves_page_provenance():
    payload = worker_input()
    payload["dataset"]["records"] = [
        {"source_id": "report-1", "text": f"FULL_CORPUS page {n}" * 500}
        for n in range(193)
    ]
    before = copy.deepcopy(payload)
    retriever = FakeRetriever()
    prepared = MarketRagPreparer(retriever).prepare(payload)
    assert payload == before
    assert len(prepared["dataset"]["records"]) == 4
    assert {r["dimension"] for r in prepared["dataset"]["records"]} == set(DIMENSIONS)
    assert "FULL_CORPUS" not in json.dumps(prepared["dataset"])
    assert len(retriever.calls) == 8
    assert all(top_k == 5 for _, top_k in retriever.calls)
    reasoner = CapturingReasoner()
    retriever.calls.clear()
    result = MarketNode(reasoner, preparer=MarketRagPreparer(retriever))(payload)
    assert len(retriever.calls) == 8
    WorkerResult.model_validate(result)
    assert result["dataset_version"] == "dataset-v1"
    assert all(e["page"] == 7 and "reports/report.pdf page 7" in e["locator"] for e in result["evidence"])
    for evidence, _ in reasoner.inputs.values():
        assert len(evidence) == 1
        assert evidence[0]["excerpt"] == "Selected industry evidence."
        assert "statement" not in evidence[0]


def test_each_dimension_has_independent_count_and_character_limits():
    retriever = FakeRetriever([
        chunk("best", text="A" * 200, score=0.95),
        chunk("best", text="duplicate", score=0.1),
        chunk("second", text="B" * 200, score=0.9),
        chunk("third", text="C" * 200, score=0.8),
    ])
    config = replace(MarketConfig(), max_retrieved_chunks_per_dimension=2,
                     max_chars_per_chunk=40, max_chars_per_dimension=55)
    prepared = MarketRagPreparer(retriever, config=config).prepare(worker_input())
    for dim in DIMENSIONS:
        records = [r for r in prepared["dataset"]["records"] if r["dimension"] == dim]
        assert [r["chunk_id"] for r in records] == ["best", "second"]
        assert [len(r["text"]) for r in records] == [40, 15]


def test_retrieval_filters_future_and_other_company_material():
    retriever = FakeRetriever([
        chunk("valid"), chunk("future", published_at="2027-01-01"),
        chunk("other-company", company_id="other"), chunk("other-agent", agent="risk"),
        chunk("not-declared", document_id="other-report"), chunk("no-page", page=None),
    ])
    prepared = MarketRagPreparer(retriever).prepare(worker_input())
    assert {r["chunk_id"] for r in prepared["dataset"]["records"]} == {"valid"}
    payload = worker_input()
    payload["dataset"]["sources"][0]["published_at"] = "2027-01-01"
    assert MarketRagPreparer(retriever).prepare(payload)["dataset"]["records"] == []


def test_empty_retrieval_never_falls_back_to_supplied_pdf_pages():
    prepared = MarketRagPreparer(FakeRetriever([])).prepare(worker_input())
    assert prepared["dataset"]["records"] == []
    reasoner = FakeReasoner()
    result = MarketNode(reasoner, use_rag=False)(prepared)
    assert reasoner.dimension_calls == []
    assert result["data"]["overall_market_score"] is None
    assert result["evidence"] == []


def test_retrieval_failure_stops_preparation():
    class FailedRetriever:
        def retrieve(self, *args, **kwargs):
            raise RuntimeError("index query failed")

    with pytest.raises(MarketRagPreparationError, match="no full-PDF fallback"):
        MarketRagPreparer(FailedRetriever()).prepare(worker_input())


def test_missing_index_does_not_create_empty_collection_or_disable_rag(monkeypatch, tmp_path):
    import agents.market.retrieval as retrieval

    missing = tmp_path / "missing"
    monkeypatch.setattr(retrieval, "_DEFAULT_PREPARER", None)
    monkeypatch.setenv("MARKET_RAG_VECTORSTORE_DIR", str(missing))
    with pytest.raises(MarketRagPreparationError, match="build_index"):
        retrieval.default_market_preparer()
    assert not missing.exists()


def test_company_context_is_bounded_and_not_mixed_with_industry_evidence():
    payload = worker_input(profile={"company_id": "company-1", "company_name": "Acme"})
    reasoner = CapturingReasoner()
    prepared = MarketRagPreparer(FakeRetriever()).prepare(payload)
    MarketNode(reasoner, use_rag=False)(prepared)
    assert reasoner.supplement_calls == []

    payload["dataset"]["records"] = [{
        "source_id": "report-1", "company_id": "company-1",
        "text": "Acme has an annual subscription. " * 1000,
    }]
    config = replace(MarketConfig(), max_context_chars=100, max_chars_per_chunk=100)
    prepared = MarketRagPreparer(FakeRetriever(), config=config).prepare(payload)
    context = [r for r in prepared["dataset"]["records"] if r["dimension"] == "context"]
    assert sum(len(r["text"]) for r in context) == 100
    reasoner = CapturingReasoner()
    MarketNode(reasoner, use_rag=False)(prepared)
    assert len(reasoner.supplement_calls) == 1
    assert reasoner.supplement_calls[0][2][0]["content"] == context[0]["text"]
    assert all(len(evidence) == 1 for evidence, _ in reasoner.inputs.values())


def test_recollected_market_record_supplements_company_context():
    payload = worker_input(profile={"company_id": "company-1", "company_name": "Acme"})
    payload["dataset"]["records"] = [
        {"source_id": "report-1", "company_ids": ["company-1"], "tags": ["market", "recollected"],
         "text": "Acme sells to hospital procurement teams."},
        {"source_id": "report-1", "company_ids": ["company-1"], "tags": ["traction", "recollected"],
         "text": "Traction-only record."},
        {"source_id": "report-1", "company_ids": ["other"], "tags": ["market", "recollected"],
         "text": "Other company record."},
        {"source_id": "report-1", "company_ids": [], "text": "Shared industry page."},
    ]
    prepared = MarketRagPreparer(FakeRetriever()).prepare(payload)
    context = [r["text"] for r in prepared["dataset"]["records"] if r["dimension"] == "context"]
    assert context == ["Acme sells to hospital procurement teams."]


def test_review_never_sends_prior_pdf_evidence_back_to_llm():
    payload = worker_input()
    payload["previous_result"] = {
        "company_id": "company-1", "agent": "market", "result_version": 1,
        "evidence": [{"excerpt": "PRIOR_FULL_PDF" * 10000}],
        "sources": [{"content": "PRIOR_FULL_PDF" * 10000}],
        "data": {"dimensions": {"demand": {"score": None, "score_status": "unknown", "rationale": "needs review"}}},
    }
    payload["review_request"] = {
        "request_id": "review-1", "dimensions": ["monetization"],
        "questions": [{"text": "추가 지불 경로 근거"}],
    }
    retriever = FakeRetriever()
    prepared = MarketRagPreparer(retriever).prepare(payload)
    assert sum("추가 지불 경로 근거" in q for q, _ in retriever.calls) == 1
    reasoner = CapturingReasoner()
    result = MarketNode(reasoner, use_rag=False)(prepared)
    assert result["result_version"] == 2
    for _, review in reasoner.inputs.values():
        assert "PRIOR_FULL_PDF" not in json.dumps(review)
        assert review["previous_result"]["dimensions"]["demand"]["rationale"] == "needs review"


def test_queries_use_primary_product_and_do_not_require_a_reasoning_call():
    payload = worker_input(profile={
        "company_id": "company-1", "company_name": "Acme", "primary_product_id": "drug",
        "products": [{"product_id": "ct", "product_type": "CT imaging"},
                     {"product_id": "drug", "product_type": "Drug Safety", "buyer": "Hospitals"}],
    })
    queries = market_queries(WorkerInput.model_validate(payload))
    assert all("Drug Safety" in q and "CT imaging" not in q for values in queries.values() for q in values)


def test_existing_orchestrator_calls_worker_rag_without_graph_changes():
    from agents.investment_review.nodes import build_company_graph

    reasoner, retriever = CapturingReasoner(), FakeRetriever()
    market = MarketNode(reasoner, preparer=MarketRagPreparer(retriever))
    graph = build_company_graph({
        "clinical": lambda _: {}, "market": market, "traction": lambda _: {}, "risk": lambda _: {},
    })
    result = graph.invoke({
        "run_id": "graph-run", "as_of": "2026-09-30", "company_id": "company-1",
        "company_profile": complete_profile(),
    })
    market_result = result["market_analysis"]
    WorkerResult.model_validate(market_result)
    assert market_result["company_id"] == "company-1"
    assert market_result["dataset_version"] == "market-rag-v1"
    assert len(retriever.calls) == 8
    assert len(reasoner.dimension_calls) == 4


def test_legacy_review_uses_same_worker_and_fixed_pdf_index():
    reasoner, retriever = CapturingReasoner(), FakeRetriever()
    node = MarketNode(reasoner, preparer=MarketRagPreparer(retriever))
    state = {
        "run_id": "review", "as_of": "2026-09-30",
        "company_profile": complete_profile(), "review_requests": [],
        "market_analysis": None,
    }
    first = node(state)["market_analysis"]
    state["market_analysis"] = first
    state["review_requests"] = [{
        "request_id": "rr1", "target_agent": "market", "company_id": "company-1",
        "dimensions": ["monetization"], "questions": [{"text": "추가 과금 근거"}],
    }]
    second = node(state)["market_analysis"]
    WorkerResult.model_validate(second)
    assert second["result_version"] == 2
    assert second["review_responses"][0]["request_id"] == "rr1"
    assert len(retriever.calls) == 17
    assert len(reasoner.dimension_calls) == 8


def test_relative_index_path_uses_project_root(monkeypatch, tmp_path):
    from agents.market.config import PROJECT_ROOT

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MARKET_RAG_VECTORSTORE_DIR", "data/vectorstore/market")
    assert MarketConfig.from_env().vectorstore_dir == PROJECT_ROOT / "data/vectorstore/market"


def test_cli_prepares_rag_by_default_and_can_export_selected_input(monkeypatch, tmp_path):
    import agents.market.__main__ as cli

    inp, out, selected = (tmp_path / name for name in ("input.json", "output.json", "selected.json"))
    inp.write_text(json.dumps(worker_input()))
    retriever = FakeRetriever()
    monkeypatch.setattr("agents.market.retrieval.default_market_preparer", lambda: MarketRagPreparer(retriever))
    monkeypatch.setattr(sys, "argv", ["market", "--input", str(inp), "--output", str(out),
                                    "--prepared-output", str(selected), "--demo"])
    cli.main()
    assert len(retriever.calls) == 8
    assert len(json.loads(selected.read_text())["dataset"]["records"]) == 4
    WorkerResult.model_validate(json.loads(out.read_text()))


def test_cli_failure_does_not_evaluate_full_pdf_input(monkeypatch, tmp_path):
    import agents.market.__main__ as cli

    inp, out = tmp_path / "input.json", tmp_path / "output.json"
    inp.write_text(json.dumps(worker_input()))

    def unavailable():
        raise MarketRagPreparationError("index unavailable")

    monkeypatch.setattr("agents.market.retrieval.default_market_preparer", unavailable)
    monkeypatch.setattr(MarketNode, "evaluate_prepared", lambda *a: pytest.fail("evaluation must not be called"))
    monkeypatch.setattr(sys, "argv", ["market", "--input", str(inp), "--output", str(out), "--demo"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize("name", ["retrieval_top_k", "max_retrieved_chunks_per_dimension", "max_chars_per_chunk", "max_chars_per_dimension", "max_context_chars"])
def test_rag_limits_must_be_positive(name):
    with pytest.raises(ValueError, match="positive"):
        replace(MarketConfig(), **{name: 0})
