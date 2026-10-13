from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

import agents.market.agent as market_agent_module
from agents.market import MarketNode, WorkerResult
from agents.market.providers import (
    OpenAIStructuredMarketReasoner,
)
from agents.market.schema import (
    AssessmentCheck,
    CheckStatus,
    DimensionDraft,
    MarketMetric,
    MarketScopeDraft,
    SupplementalDraft,
    SupplementalFact,
)


def complete_profile() -> dict:
    return {
        "company_id": "company-1",
        "company_name": "Acme Health AI",
        "product_type": "clinical decision support software",
        "intended_use": "identify medication safety risks",
        "target_customer": "general hospitals",
        "buyer": "hospital procurement department",
        "primary_user": "hospital pharmacists",
        "business_model": "annual enterprise subscription",
        "geography": ["KR"],
        "core_problem": "manual medication review workload",
    }


class FakeReasoner:
    def __init__(self):
        self.dimension_calls: list[str] = []

    def supplement(self, profile, required_fields, sources):
        source_id = sources[0]["source_id"] if sources else ""
        value_by_field = {
            "product_type": "clinical decision support software",
            "intended_use": "identify medication safety risks",
            "target_customer": "general hospitals",
            "buyer": "hospital procurement teams",
            "primary_user": "hospital pharmacists",
            "business_model": "annual subscription",
            "geography": ["KR"],
            "core_problem": "manual medication review workload",
        }
        return SupplementalDraft(
            facts=[
                SupplementalFact(
                    field=field,
                    value=value_by_field[field],
                    source_ids=[source_id] if source_id else [],
                    excerpt="annual subscription",
                )
                for field in required_fields
            ],
            unresolved_fields=[],
        )

    def define_scope(self, profile, supplemental, *, as_of):
        return MarketScopeDraft(
            segment="clinical decision support software",
            geography=["KR"],
            target_customer="general hospitals",
            buyer="hospital procurement departments",
            primary_user="hospital pharmacists",
            product_type="clinical decision support software",
            intended_use="medication safety review",
            core_problem="manual review workload",
            business_model="annual enterprise subscription",
            base_year=int(as_of[:4]),
            forecast_period=f"{as_of[:4]}-{int(as_of[:4]) + 5}",
        )

    def analyze_dimension(
        self,
        dimension,
        scope,
        profile,
        supplemental,
        evidence,
        rubric,
        review_request=None,
    ):
        self.dimension_calls.append(dimension)
        evidence_id = evidence[0]["evidence_id"]
        if dimension == "size_growth":
            return DimensionDraft(
                rationale="동일 세그먼트·지역의 CAGR 근거 확인",
                evidence_ids=[evidence_id],
                market_metrics=[
                    MarketMetric(
                        metric_id="cagr-kr-cdss-2025-2030",
                        metric_type="cagr",
                        value=0.18,
                        unit="ratio",
                        geography="KR",
                        segment="clinical decision support software",
                        base_year=2026,
                        start_year=2026,
                        end_year=2031,
                        method="report forecast",
                        evidence_ids=[evidence_id],
                    )
                ],
            )
        checks = [
            AssessmentCheck(
                check_id=item["check_id"],
                status=CheckStatus.YES,
                rationale="원문 근거에서 확인",
                evidence_ids=[evidence_id],
            )
            for item in rubric["checks"]
        ]
        return DimensionDraft(
            rationale=f"{dimension} 근거 확인",
            evidence_ids=[evidence_id],
            checks=checks,
        )


def worker_input(profile=None, *, records=True):
    return {
        "run_id": "run-2026-09-30",
        "company_id": "company-1",
        "as_of": "2026-09-30",
        "schema_version": "2.0",
        "criteria_version": "proposed-2.0",
        "company_profile": profile if profile is not None else complete_profile(),
        "dataset": {
            "version": "dataset-v1",
            "sources": [
                {
                    "source_id": "report-1",
                    "title": "Healthcare AI Adoption Report",
                    "url": "https://research.example/report",
                    "publisher": "Research Institute",
                    "published_at": "2025-06-01",
                    "checked_at": "2025-06-02",
                    "source_type": "research",
                    "region": "KR",
                    "segment": "clinical decision support software",
                }
            ]
            if records
            else [],
            "records": [
                {
                    "record_id": "report-1:page-7",
                    "source_id": "report-1",
                    "page": 7,
                    "locator": "page 7",
                    "text": "Acme has an annual subscription to hospital procurement teams. Comparable CAGR is 18 percent; adoption demand and monetization evidence.",
                }
            ]
            if records
            else [],
        },
        "previous_result": None,
        "review_request": None,
    }


@pytest.fixture(autouse=True)
def no_external_data_calls(monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("Worker attempted an external data call")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def test_single_pass_contract_and_existing_scoring():
    reasoner = FakeReasoner()
    result = MarketNode(reasoner, use_rag=False)(worker_input())
    assert Counter(reasoner.dimension_calls) == Counter(
        {d: 1 for d in ("size_growth", "demand", "commercialization", "monetization")}
    )
    assert set(result) == {
        "run_id",
        "company_id",
        "as_of",
        "schema_version",
        "criteria_version",
        "agent",
        "dataset_version",
        "result_version",
        "analysis_status",
        "data",
        "sources",
        "evidence",
        "findings",
        "missing_items",
        "coverage",
        "review_responses",
        "errors",
    }
    assert result["agent"] == "market"
    assert result["dataset_version"] == "dataset-v1"
    assert result["analysis_status"] == "complete"
    assert result["data"]["criteria_inputs"]["C2"]["score"] == 4
    assert result["data"]["criteria_inputs"]["C3"]["score"] == 5
    assert result["data"]["criteria_inputs"]["C4"]["score"] == 5
    assert result["data"]["overall_market_score"] == pytest.approx(4.75)
    assert {s["source_id"] for s in result["sources"]} == {"report-1"}
    assert all(
        e["source_ids"] == ["report-1"] and e["locator"] == "page 7"
        for e in result["evidence"]
    )
    WorkerResult.model_validate(result)


def test_missing_data_returns_contract_missing_items_and_null_scores():
    reasoner = FakeReasoner()
    result = MarketNode(reasoner, use_rag=False)(worker_input(records=False))
    assert reasoner.dimension_calls == []
    assert result["analysis_status"] == "partial"
    assert result["data"]["overall_market_score"] is None
    assert all(
        d["score"] is None and d["score_status"] == "unknown"
        for d in result["data"]["dimensions"].values()
    )
    assert all(
        set(m)
        == {
            "missing_id",
            "item",
            "reason",
            "related_criterion_ids",
            "required_for_assessment",
        }
        for m in result["missing_items"]
    )
    assert all(m["required_for_assessment"] for m in result["missing_items"])


def test_missing_profile_is_supplemented_only_from_supplied_data():
    profile = {"company_id": "company-1", "company_name": "Acme Health AI"}
    result = MarketNode(FakeReasoner(), use_rag=False)(worker_input(profile))
    facts = result["data"]["supplemental_context"]["facts"]
    assert facts and all(f["source_ids"] == ["report-1"] for f in facts)
    assert not any(m["item"] == "buyer" for m in result["missing_items"])


def test_reexecution_always_evaluates_once_even_with_repeated_review_id_and_high_round():
    reasoner = FakeReasoner()
    node = MarketNode(reasoner, use_rag=False)
    payload = worker_input()
    first = node(payload)
    payload["previous_result"] = first
    payload["run_id"] = "run-next"
    payload["review_request"] = {
        "request_id": "review-1",
        "company_id": "company-1",
        "target_agent": "market",
        "review_round": 99,
        "dimensions": ["demand"],
    }
    reasoner.dimension_calls.clear()
    second = node(payload)
    assert len(reasoner.dimension_calls) == 4
    assert second["result_version"] == 2
    assert second["run_id"] == "run-next"
    assert second["review_responses"][0]["request_id"] == "review-1"
    payload["previous_result"] = second
    third = node(payload)
    assert len(reasoner.dimension_calls) == 8
    assert third["result_version"] == 3


def test_reexecution_with_empty_dataset_never_reuses_old_sources_or_scores():
    node = MarketNode(FakeReasoner(), use_rag=False)
    first = node(worker_input())
    payload = worker_input(records=False)
    payload["dataset"]["version"] = "empty-v2"
    payload["previous_result"] = first
    result = node(payload)
    assert result["sources"] == [] and result["evidence"] == []
    assert result["dataset_version"] == "empty-v2" and result["result_version"] == 2
    assert result["data"]["overall_market_score"] is None
    assert result["analysis_status"] == "partial"


def test_failure_has_no_retry_and_keeps_other_domain_results():
    class Failing(FakeReasoner):
        def analyze_dimension(self, dimension, *args, **kwargs):
            if dimension == "demand":
                self.dimension_calls.append(dimension)
                raise RuntimeError("provider unavailable")
            return super().analyze_dimension(dimension, *args, **kwargs)

    reasoner = Failing()
    result = MarketNode(reasoner, use_rag=False)(worker_input())
    assert len(reasoner.dimension_calls) == 4
    assert reasoner.dimension_calls.count("demand") == 1
    assert result["data"]["dimensions"]["demand"]["score"] is None
    assert result["data"]["dimensions"]["monetization"]["score"] == 5
    assert result["errors"] == [
        {"stage": "evaluation", "dimension": "demand", "error_type": "RuntimeError"}
    ]
    assert result["analysis_status"] == "partial"


def test_invalid_evidence_ids_cannot_score_checks():
    class BadEvidence(FakeReasoner):
        def analyze_dimension(self, dimension, *args, **kwargs):
            draft = super().analyze_dimension(dimension, *args, **kwargs)
            for check in draft.checks:
                check.evidence_ids = ["not-provided"]
            return draft

    result = MarketNode(BadEvidence(), use_rag=False)(worker_input())
    assert result["data"]["dimensions"]["demand"]["score"] is None
    assert all(
        c["status"] == "unknown"
        for c in result["data"]["dimensions"]["demand"]["checks"]
    )


def test_mismatched_cagr_period_remains_unknown():
    class WrongPeriod(FakeReasoner):
        def analyze_dimension(self, dimension, *args, **kwargs):
            draft = super().analyze_dimension(dimension, *args, **kwargs)
            for metric in draft.market_metrics:
                metric.start_year, metric.end_year = 2025, 2030
            return draft

    result = MarketNode(WrongPeriod(), use_rag=False)(worker_input())
    assert result["data"]["criteria_inputs"]["C2"]["score"] is None


def test_cagr_keeps_metric_evidence_when_top_level_ids_are_omitted():
    class MetricEvidenceOnly(FakeReasoner):
        def analyze_dimension(self, dimension, *args, **kwargs):
            draft = super().analyze_dimension(dimension, *args, **kwargs)
            if dimension == "size_growth":
                draft.evidence_ids = []
            return draft

    result = MarketNode(MetricEvidenceOnly(), use_rag=False)(worker_input())
    c2 = result["data"]["criteria_inputs"]["C2"]
    assert c2["score"] == 4 and c2["evidence_ids"]
    assert set(c2["evidence_ids"]) <= {e["evidence_id"] for e in result["evidence"]}


def test_all_domain_call_failures_return_failed_without_retry():
    class FailedReasoner(FakeReasoner):
        def analyze_dimension(self, dimension, *args, **kwargs):
            self.dimension_calls.append(dimension)
            raise RuntimeError("provider unavailable")

    reasoner = FailedReasoner()
    result = MarketNode(reasoner, use_rag=False)(worker_input())
    assert len(reasoner.dimension_calls) == 4
    assert len(result["errors"]) == 4
    assert result["analysis_status"] == "failed"


def test_wrong_profile_excerpt_cannot_fill_missing_context():
    class UnlinkedProfile(FakeReasoner):
        def supplement(self, *args, **kwargs):
            draft = super().supplement(*args, **kwargs)
            for fact in draft.facts:
                fact.excerpt = "quotation absent from all supplied material"
            return draft

    result = MarketNode(UnlinkedProfile(), use_rag=False)(worker_input(profile={}))
    assert not result["data"]["supplemental_context"]["facts"]
    assert any(m["item"] == "buyer" for m in result["missing_items"])


def test_dataset_filters_future_and_other_company_records():
    payload = worker_input()
    payload["dataset"]["sources"].append(
        {"source_id": "future", "published_at": "2027-01-01"}
    )
    payload["dataset"]["records"] = [
        {"source_id": "future", "text": "future material"},
        {
            "source_id": "report-1",
            "company_id": "another-company",
            "text": "another company",
        },
        {"source_id": "not-provided", "text": "unknown source"},
    ]
    reasoner = FakeReasoner()
    result = MarketNode(reasoner, use_rag=False)(payload)
    assert not reasoner.dimension_calls
    assert not result["evidence"]
    assert {s["source_id"] for s in result["sources"]} == {"report-1"}
    assert result["errors"][0]["code"] == "unknown_source_id"


def test_input_is_not_mutated_and_duplicate_records_do_not_duplicate_evidence():
    payload = worker_input()
    payload["dataset"]["records"].append(
        copy.deepcopy(payload["dataset"]["records"][0])
    )
    before = copy.deepcopy(payload)
    result = MarketNode(FakeReasoner(), use_rag=False)(payload)
    assert payload == before
    assert len(result["evidence"]) == 4


def test_worker_never_imports_external_search_on_reexecution(monkeypatch, tmp_path):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("tavily") or name.startswith("playwright"):
            raise AssertionError(f"Worker imported external search or collection: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(market_agent_module, "_DEFAULT_AGENT", None)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MARKET_MODEL", "")
    monkeypatch.setenv("TAVILY_API_KEY", "unused-key")
    monkeypatch.setenv("MARKET_RAG_VECTORSTORE_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=4) as pool:
        nodes = list(
            pool.map(lambda _: market_agent_module.default_market_agent(), range(4))
        )
    assert len({id(node) for node in nodes}) == 1
    class FixedLocalContext:
        calls = 0

        def prepare(self, payload):
            self.calls += 1
            return payload

    preparer = FixedLocalContext()
    nodes[0].preparer = preparer
    assert nodes[0].use_rag is True
    payload = worker_input()
    payload["previous_result"] = nodes[0](payload)
    result = nodes[0](payload)
    assert result["result_version"] == 2
    assert preparer.calls == 2
    monkeypatch.setattr(market_agent_module, "_DEFAULT_AGENT", None)


def test_single_node_graph_has_no_collection_or_retry_edges():
    node = MarketNode(FakeReasoner(), use_rag=False)
    graph = node.build_graph()
    assert {(e.source, e.target) for e in graph.get_graph().edges} == {
        ("__start__", "evaluate"),
        ("evaluate", "__end__"),
    }
    assert graph.invoke(worker_input())["agent"] == "market"


def test_cli_emits_direct_worker_result(tmp_path, monkeypatch):
    from agents.market.__main__ import main

    inp, out = tmp_path / "input.json", tmp_path / "output.json"
    inp.write_text(json.dumps(worker_input()), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["market", "--input", str(inp), "--output", str(out), "--demo", "--prepared-input"]
    )
    main()
    result = json.loads(out.read_text())
    assert result["agent"] == "market" and "market_analysis" not in result
    WorkerResult.model_validate(result)


def test_model_provider_disables_sdk_retries(monkeypatch):
    import langchain_openai

    called = {}

    class Chat:
        def __init__(self, **kwargs):
            called.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Chat)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    OpenAIStructuredMarketReasoner(model="test-model")
    assert called["max_retries"] == 0


@pytest.mark.parametrize("field,value", [("company_id", "other"), ("agent", "risk")])
def test_rejects_previous_result_identity_mismatch(field, value):
    payload = worker_input()
    payload["previous_result"] = {
        "company_id": "company-1",
        "agent": "market",
        "result_version": 1,
        field: value,
    }
    with pytest.raises(ValueError):
        MarketNode(use_rag=False)(payload)
