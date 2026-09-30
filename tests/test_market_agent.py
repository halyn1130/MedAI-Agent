from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import agents.market.agent as market_agent_module
from agents.market.agent import MarketAgent
from agents.market.providers import WebSearchResult
from agents.market.schema import (
    AssessmentCheck,
    CheckStatus,
    DimensionDraft,
    MarketMetric,
    MarketScopeDraft,
    SupplementalDraft,
    SupplementalFact,
)
from rag.market.retriever import EmptyRetriever, RetrievedChunk


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


class FakeSearch:
    def __init__(self):
        self.calls: list[str] = []

    def search(self, query: str, *, as_of: str, max_results: int = 5):
        self.calls.append(query)
        return [
            WebSearchResult(
                title="Acme product page",
                url=f"https://acme.example/{len(self.calls)}",
                publisher="Acme",
                content="Acme sells an annual subscription to hospital procurement teams in Korea.",
                published_at="2025-01-01",
            )
        ]


class FakeRetriever:
    def __init__(self):
        self.calls: list[str] = []

    def retrieve(self, query: str, *, top_k: int = 5):
        self.calls.append(query)
        suffix = hashlib.sha256(query.encode()).hexdigest()[:10]
        return [
            RetrievedChunk(
                chunk_id=f"chunk-{suffix}",
                text=(
                    "The comparable healthcare AI segment has an 18 percent CAGR. "
                    "Customers report unmet need, procurement demand, feasible workflow integration, "
                    "and recurring enterprise subscription purchasing."
                ),
                metadata={
                    "document_id": "industry-report-1",
                    "title": "Healthcare AI Adoption Report",
                    "publisher": "Public Research Institute",
                    "published_at": "2025-06-01",
                    "region": "KR",
                    "segment": "clinical decision support software",
                    "source_type": "industry_report",
                    "source_url": "https://research.example/report",
                    "path": "reports/adoption.pdf",
                    "page": 7,
                },
                score=0.91,
            )
        ]


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


def build_agent(search=None, retriever=None, reasoner=None):
    return MarketAgent(
        search or FakeSearch(), retriever or FakeRetriever(), reasoner or FakeReasoner()
    )


def test_demo_cli_writes_documented_output_contract(tmp_path, monkeypatch):
    from agents.market.__main__ import main

    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(
        json.dumps({"as_of": "2026-09-30", "company_profile": complete_profile()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "python -m agents.market",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--demo",
        ],
    )

    main()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(payload) == {"market_analysis", "review_response"}
    assert payload["market_analysis"]["agent"] == "market"
    assert payload["market_analysis"]["company_id"] == "company-1"
    assert payload["review_response"] is None


def test_default_agent_is_shared_across_parallel_company_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(market_agent_module, "_DEFAULT_AGENT", None)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MARKET_MODEL", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("MARKET_RAG_VECTORSTORE_DIR", str(tmp_path / "absent"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        agents = list(
            pool.map(lambda _: market_agent_module.default_market_agent(), range(4))
        )

    assert len({id(agent) for agent in agents}) == 1
    monkeypatch.setattr(market_agent_module, "_DEFAULT_AGENT", None)


def test_complete_profile_skips_web_and_runs_four_dimensions():
    search = FakeSearch()
    reasoner = FakeReasoner()
    agent = build_agent(search=search, reasoner=reasoner)

    result = agent.run(complete_profile(), as_of="2026-09-30").market_analysis

    assert search.calls == []
    assert sorted(reasoner.dimension_calls) == [
        "commercialization",
        "demand",
        "monetization",
        "size_growth",
    ]
    assert result.data.dimensions.size_growth.score == 4
    assert result.data.dimensions.demand.score == 5
    assert result.data.overall_market_score == pytest.approx(4.75)
    assert result.data.criteria_inputs.C2.score == 4
    assert result.data.criteria_inputs.C3.score == 5
    assert result.data.criteria_inputs.C4.score == 5


def test_missing_profile_context_triggers_web_search_only_then_preserves_sources():
    search = FakeSearch()
    profile = {"company_id": "company-1", "company_name": "Acme Health AI"}

    result = build_agent(search=search).run(profile, as_of="2026-09-30").market_analysis

    assert search.calls
    assert result.web_search_log
    assert any(item.url.startswith("https://acme.example/") for item in result.sources)
    assert result.data.supplemental_context.facts


def test_each_dimension_node_writes_only_its_own_result_key_and_shared_lists():
    agent = build_agent()
    state = {
        "company_id": "company-1",
        "as_of": "2026-09-30",
        "company_profile": complete_profile(),
        "market_scope": FakeReasoner()
        .define_scope(complete_profile(), {}, as_of="2026-09-30")
        .model_dump(mode="json"),
        "market_queries": {
            "size_growth": ["query"],
            "demand": ["query"],
            "commercialization": ["query"],
            "monetization": ["query"],
        },
        "review_request": None,
    }
    result_keys = {
        "size_growth_result",
        "demand_result",
        "commercialization_result",
        "monetization_result",
    }
    for dimension in ("size_growth", "demand", "commercialization", "monetization"):
        update = agent._dimension_node(dimension)(state)
        assert set(update) & result_keys == {f"{dimension}_result"}


def test_graph_fans_out_four_dimension_nodes_and_fans_in_to_merge():
    graph = build_agent().build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    for dimension in ("size_growth", "demand", "commercialization", "monetization"):
        node = f"analyze_{dimension}"
        assert ("query_generation", node) in edges
        assert (node, "merge") in edges


def test_rag_evidence_keeps_source_and_page_locator():
    result = build_agent().run(complete_profile(), as_of="2026-09-30").market_analysis

    assert result.evidence
    assert {item.page for item in result.evidence} == {7}
    assert all(item.locator == "page 7" for item in result.evidence)
    assert all(item.source_ids for item in result.evidence)
    assert result.findings[0].evidence_ids


def test_missing_rag_evidence_is_unknown_not_zero():
    result = (
        build_agent(retriever=EmptyRetriever())
        .run(complete_profile(), as_of="2026-09-30")
        .market_analysis
    )

    for dimension in result.data.dimensions.model_dump().values():
        assert dimension["score_status"] == "unknown"
        assert dimension["score"] is None
    assert result.data.overall_market_score is None


def test_review_reruns_only_requested_dimension_and_stops_after_round_one():
    reasoner = FakeReasoner()
    agent = build_agent(reasoner=reasoner)
    first = agent.run(complete_profile(), as_of="2026-09-30").market_analysis
    reasoner.dimension_calls.clear()
    request = {
        "request_id": "review-1",
        "company_id": "company-1",
        "target_agent": "market",
        "criterion_id": "C3",
        "reason": "수요 근거 보완",
        "blocker": "고객 수요 확인 필요",
        "questions": [{"question_id": "q1", "text": "고객 수요 근거를 보완해줘"}],
        "review_round": 1,
        "dimensions": ["demand"],
        "source_route": "rag",
    }

    reviewed = agent.run(
        complete_profile(),
        as_of="2026-09-30",
        review_request=request,
        previous=first,
    )

    assert reasoner.dimension_calls == ["demand"]
    assert reviewed.market_analysis.result_version == 2
    assert reviewed.review_response is not None
    assert reviewed.review_response.request_id == "review-1"

    second_round = {**request, "request_id": "review-2", "review_round": 2}
    state_update = agent(
        {
            "company_profile": complete_profile(),
            "as_of": "2026-09-30",
            "market_analysis": reviewed.market_analysis.model_dump(mode="json"),
            "review_requests": [second_round],
        }
    )
    assert state_update == {}


def test_review_removes_resolved_missing_item_for_target_dimension():
    profile = complete_profile()
    first = (
        build_agent(retriever=EmptyRetriever())
        .run(profile, as_of="2026-09-30")
        .market_analysis
    )
    request = {
        "request_id": "review-resolved-missing",
        "company_id": "company-1",
        "target_agent": "market",
        "criterion_id": "C3",
        "reason": "수요 근거 보완",
        "blocker": "수요 근거 누락",
        "questions": [{"question_id": "q1", "text": "수요 근거를 다시 찾아줘"}],
        "review_round": 1,
        "dimensions": ["demand"],
        "source_route": "rag",
    }

    reviewed = (
        build_agent()
        .run(
            profile,
            as_of="2026-09-30",
            review_request=request,
            previous=first,
        )
        .market_analysis
    )

    assert reviewed.data.dimensions.demand.score_status.value == "scored"
    assert not [item for item in reviewed.missing_items if item.dimension == "demand"]
    assert [
        item for item in reviewed.missing_items if item.dimension == "commercialization"
    ]


def test_invalid_evidence_id_cannot_produce_a_known_check_score():
    class BadEvidenceReasoner(FakeReasoner):
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
            if dimension == "size_growth":
                return super().analyze_dimension(
                    dimension,
                    scope,
                    profile,
                    supplemental,
                    evidence,
                    rubric,
                    review_request,
                )
            return DimensionDraft(
                rationale="잘못된 근거 ID",
                checks=[
                    AssessmentCheck(
                        check_id=item["check_id"],
                        status=CheckStatus.YES,
                        rationale="잘못 연결됨",
                        evidence_ids=["unknown-evidence"],
                    )
                    for item in rubric["checks"]
                ],
            )

    result = (
        build_agent(reasoner=BadEvidenceReasoner())
        .run(complete_profile(), as_of="2026-09-30")
        .market_analysis
    )
    assert result.data.dimensions.demand.score_status.value == "unknown"
    assert result.data.dimensions.demand.score is None


def test_cagr_from_a_different_period_is_not_scored():
    class MismatchedPeriodReasoner(FakeReasoner):
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
            draft = super().analyze_dimension(
                dimension,
                scope,
                profile,
                supplemental,
                evidence,
                rubric,
                review_request,
            )
            if dimension == "size_growth":
                draft.market_metrics[0].start_year = 2025
                draft.market_metrics[0].end_year = 2030
            return draft

    result = (
        build_agent(reasoner=MismatchedPeriodReasoner())
        .run(complete_profile(), as_of="2026-09-30")
        .market_analysis
    )

    assert result.data.dimensions.size_growth.score_status.value == "unknown"
    assert result.data.dimensions.size_growth.score is None
    assert result.data.dimensions.size_growth.missing_item_ids
