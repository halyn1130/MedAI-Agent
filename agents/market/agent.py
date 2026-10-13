"""One market evaluation using fixed local PDF RAG, without web search or retries."""

from __future__ import annotations

import os
import threading
from typing import Any

from .contract import (
    WorkerInput,
    make_result,
    missing_item,
    parse_input,
    provided_dataset,
    record_evidence,
    record_text,
)
from .providers import (
    ConservativeMarketReasoner,
    MarketReasoner,
    OpenAIStructuredMarketReasoner,
)
from .schema import (
    DIMENSIONS,
    AnalysisEnvelope,
    CheckStatus,
    DimensionDraft,
    MarketDimensions,
    MarketScope,
    ScoreStatus,
    SupplementalContext,
    SupplementalDraft,
)
from .scoring import (
    RUBRICS,
    comparable_cagr_value,
    criteria_inputs,
    display_overall_score,
    score_dimension,
)

FIELD_ALIASES = {
    "product_type": ("product_type", "segment", "category", "대분류"),
    "intended_use": ("intended_use", "use_case", "purpose", "description", "제품설명"),
    "target_customer": ("target_customer", "customer", "customers", "고객"),
    "buyer": ("buyer", "payer", "purchaser", "구매자", "지불주체"),
    "primary_user": ("primary_user", "user", "users", "사용자"),
    "business_model": ("business_model", "pricing_model", "revenue_model", "과금방식"),
    "geography": ("geography", "regions", "markets", "countries", "진출지역"),
    "core_problem": ("core_problem", "problem", "pain_point", "해결문제"),
}
_DEFAULT_AGENT = None
_DEFAULT_AGENT_LOCK = threading.Lock()


def _product(profile: dict[str, Any]) -> dict[str, Any]:
    products = profile.get("products") or []
    if not isinstance(products, list) or not products:
        return {}
    primary_id = profile.get("primary_product_id")
    if primary_id:
        for product in products:
            if isinstance(product, dict) and product.get("product_id") == primary_id:
                return product
    return products[0] if isinstance(products[0], dict) else {}


def _value(profile: dict[str, Any], field: str) -> Any:
    for container in (profile, _product(profile)):
        for alias in FIELD_ALIASES[field]:
            value = container.get(alias)
            if value not in (None, "", [], {}):
                return value
    return None


def required_market_context(profile: dict[str, Any]) -> list[str]:
    required = [
        "product_type",
        "intended_use",
        "target_customer",
        "buyer",
        "business_model",
        "geography",
        "core_problem",
    ]
    return [field for field in required if _value(profile, field) is None]


class MarketNode:
    def __init__(
        self, reasoner: MarketReasoner | None = None, *, use_rag: bool = True, preparer=None
    ):
        self.reasoner = reasoner or ConservativeMarketReasoner()
        self.use_rag = use_rag
        self.preparer = preparer

    @staticmethod
    def _worker_input(value: dict | WorkerInput) -> tuple[dict | WorkerInput, bool]:
        # Keep the existing 06 caller working without changing its graph or the
        # common v2 contract. Only that legacy State gets a wrapped result.
        legacy = (
            isinstance(value, dict) and "dataset" not in value
            and "company_id" not in value and "review_requests" in value
        )
        if not legacy:
            return value, False
        profile = value["company_profile"]
        cid = profile["company_id"]
        review = next((
            r for r in value.get("review_requests", [])
            if r.get("target_agent") == "market" and r.get("company_id", cid) == cid
        ), None)
        return {
            "run_id": value["run_id"], "company_id": cid, "as_of": value["as_of"],
            "company_profile": profile, "dataset": {"version": "market-rag-v1"},
            "previous_result": value.get("market_analysis"), "review_request": review,
        }, True

    def prepare_input(self, worker_input: dict | WorkerInput) -> dict:
        payload, _ = self._worker_input(worker_input)
        if self.use_rag:
            from .retrieval import prepare_market_input

            return prepare_market_input(payload, preparer=self.preparer)
        return parse_input(payload, "market").model_dump(mode="json")

    def __call__(self, worker_input: dict | WorkerInput) -> dict:
        _, legacy = self._worker_input(worker_input)
        result = self.evaluate_prepared(self.prepare_input(worker_input))
        return {"market_analysis": result} if legacy else result

    def evaluate_prepared(self, worker_input: dict | WorkerInput) -> dict:
        """Evaluate selected context once; no retrieval, collection or retries."""
        request = parse_input(worker_input, "market")
        profile = {**request.company_profile, "company_id": request.company_id}
        sources, records, errors = provided_dataset(request, "market")
        source_map = {s["source_id"]: s for s in sources}
        required = required_market_context(profile)
        context = SupplementalContext(unresolved_fields=required)
        context_records = [r for r in records if r.get("dimension") in (None, "context")]
        if required and context_records:
            try:
                material = [
                    {
                        **s,
                        "content": "\n".join(
                            record_text(r)
                            for r in context_records
                            if s["source_id"] in r["source_ids"]
                        ),
                    }
                    for s in sources
                ]
                draft = SupplementalDraft.model_validate(
                    self.reasoner.supplement(profile, required, material)
                )
                source_text = {s["source_id"]: s["content"] for s in material}
                facts = [
                    f
                    for f in draft.facts
                    if f.field in required
                    and f.source_ids
                    and set(f.source_ids) <= source_map.keys()
                    and f.excerpt
                    and any(f.excerpt in source_text[s] for s in f.source_ids)
                ]
                context = SupplementalContext(
                    facts=facts,
                    unresolved_fields=[
                        f for f in required if f not in {it.field for it in facts}
                    ],
                )
            except Exception as exc:
                errors.append({"stage": "context", "error_type": type(exc).__name__})
        missing = [
            missing_item(
                request,
                "market",
                field,
                "전달된 프로필과 자료에서 시장 범위 정보를 확인할 수 없음",
            )
            for field in required
            if context.value_for(field) is None
        ]
        try:
            scope = MarketScope.model_validate(
                self.reasoner.define_scope(
                    profile, context.model_dump(mode="json"), as_of=request.as_of
                )
            )
        except Exception as exc:
            errors.append({"stage": "scope", "error_type": type(exc).__name__})
            scope = ConservativeMarketReasoner().define_scope(
                profile, context.model_dump(mode="json"), as_of=request.as_of
            )
        dimensions, evidence, findings, coverage = {}, {}, [], []
        failed_dimensions = set()
        review_context = dict(request.review_request or {})
        if request.previous_result:
            previous = request.previous_result
            review_context["previous_result"] = {
                "result_version": previous["result_version"],
                "analysis_status": previous.get("analysis_status"),
                "dimensions": {
                    name: {
                        "score": result.get("score"),
                        "score_status": result.get("score_status"),
                        "rationale": str(result.get("rationale") or "")[:2000],
                    }
                    for name, result in previous.get("data", {}).get("dimensions", {}).items()
                    if name in DIMENSIONS and isinstance(result, dict)
                },
            }
        for dimension in DIMENSIONS:
            selected = [
                r
                for r in records
                if r.get("dimension") in (None, dimension) and record_text(r)
            ]
            material = []
            seen = set()
            for r in selected:
                ev = record_evidence(request, "market", r, dimension=dimension)
                if ev["evidence_id"] in seen:
                    continue
                seen.add(ev["evidence_id"])
                evidence[ev["evidence_id"]] = ev
                src = source_map[r["source_ids"][0]]
                # Preserve full evidence in the result, but avoid sending the
                # same passage twice as both statement and excerpt to the LLM.
                prompt_evidence = dict(ev)
                if prompt_evidence["statement"] == prompt_evidence["excerpt"]:
                    prompt_evidence.pop("statement")
                material.append(
                    {
                        **prompt_evidence,
                        "document_title": src["title"],
                        "publisher": src["publisher"],
                        "published_at": src.get("published_at"),
                        "geography": r.get("geography", src.get("region")),
                        "segment": r.get("segment", src.get("segment")),
                    }
                )
            if not material:
                draft = DimensionDraft(
                    rationale="전달된 자료에 해당 평가영역의 근거가 없음",
                    missing_fields=[f"{dimension}_evidence"],
                )
            else:
                try:
                    draft = DimensionDraft.model_validate(
                        self.reasoner.analyze_dimension(
                            dimension,
                            scope.model_dump(mode="json"),
                            profile,
                            context.model_dump(mode="json"),
                            material,
                            RUBRICS[dimension],
                            review_context or None,
                        )
                    )
                except Exception as exc:
                    failed_dimensions.add(dimension)
                    errors.append(
                        {
                            "stage": "evaluation",
                            "dimension": dimension,
                            "error_type": type(exc).__name__,
                        }
                    )
                    draft = DimensionDraft(
                        rationale="평가 호출 실패", missing_fields=[dimension]
                    )
            allowed = {e["evidence_id"] for e in material}
            draft.evidence_ids = [i for i in draft.evidence_ids if i in allowed]
            for check in draft.checks:
                check.evidence_ids = [i for i in check.evidence_ids if i in allowed]
                if (
                    check.status in (CheckStatus.YES, CheckStatus.NO)
                    and not check.evidence_ids
                ):
                    check.status, check.rationale = (
                        CheckStatus.UNKNOWN,
                        "전달된 근거 ID로 확인할 수 없음",
                    )
            for metric in draft.market_metrics:
                metric.evidence_ids = [i for i in metric.evidence_ids if i in allowed]
            draft.market_metrics = [m for m in draft.market_metrics if m.evidence_ids]
            draft.evidence_ids = sorted(
                set(draft.evidence_ids)
                | {i for check in draft.checks for i in check.evidence_ids}
                | {i for metric in draft.market_metrics for i in metric.evidence_ids}
            )
            if (
                dimension == "size_growth"
                and not draft.not_applicable
                and comparable_cagr_value(draft, scope) is None
            ):
                draft.missing_fields = list(
                    dict.fromkeys(draft.missing_fields + ["comparable_cagr"])
                )
            local_missing = [
                missing_item(
                    request,
                    "market",
                    f"{dimension}:{field}",
                    draft.rationale,
                    [RUBRICS[dimension]["criterion_id"]],
                )
                for field in dict.fromkeys(draft.missing_fields)
            ]
            result = score_dimension(
                dimension, draft, [m["missing_id"] for m in local_missing], scope=scope
            )
            if result.score_status == ScoreStatus.UNKNOWN and not local_missing:
                local_missing.append(
                    missing_item(
                        request,
                        "market",
                        dimension,
                        "주어진 근거로 점수를 산출할 수 없음",
                        [RUBRICS[dimension]["criterion_id"]],
                    )
                )
                result.missing_item_ids = [m["missing_id"] for m in local_missing]
            missing.extend(local_missing)
            dimensions[dimension] = result
            findings.append(
                {
                    "finding_id": f"{request.company_id}:market:f-{dimension}",
                    "category": dimension,
                    "claim": result.rationale,
                    "evidence_ids": result.evidence_ids,
                    "uncertainty": "자료 부족"
                    if result.score_status == ScoreStatus.UNKNOWN
                    else None,
                    "related_criterion_ids": [RUBRICS[dimension]["criterion_id"]],
                }
            )
            coverage.append(
                {
                    "area": dimension,
                    "status": "insufficient_information"
                    if local_missing
                    else "reviewed",
                    "source_ids": sorted(
                        {s for e in material for s in e["source_ids"]}
                    ),
                }
            )
        dims = MarketDimensions(**dimensions)
        data = {
            "market_scope": scope.model_dump(mode="json"),
            "supplemental_context": context.model_dump(mode="json"),
            "dimensions": dims.model_dump(mode="json"),
            "criteria_inputs": criteria_inputs(dims).model_dump(mode="json"),
            "overall_market_score": display_overall_score(dims),
            "market_metrics": [
                m.model_dump(mode="json")
                for d in dimensions.values()
                for m in d.market_metrics
            ],
            "market_summary": "제공 데이터 기반 시장·사업성 평가",
            "business_concerns": list(
                dict.fromkeys(
                    c for d in dimensions.values() for c in d.business_concerns
                )
            ),
        }
        return make_result(
            request,
            "market",
            data=data,
            sources=sources,
            evidence=list(evidence.values()),
            findings=findings,
            missing=missing,
            coverage=coverage,
            errors=errors,
            failed=len(failed_dimensions) == len(DIMENSIONS),
        )

    def run(self, worker_input: dict | WorkerInput) -> AnalysisEnvelope:
        return AnalysisEnvelope.model_validate(self(worker_input))

    def build_graph(self):
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(dict)
        graph.add_node("evaluate", self)
        graph.add_edge(START, "evaluate")
        graph.add_edge("evaluate", END)
        return graph.compile()


# Compatibility names all point to the same single evaluation node.
MarketAgent = MarketNode


def default_market_agent() -> MarketNode:
    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is None:
        with _DEFAULT_AGENT_LOCK:
            if _DEFAULT_AGENT is None:
                from dotenv import load_dotenv

                load_dotenv()
                reasoner = (
                    OpenAIStructuredMarketReasoner()
                    if os.getenv("OPENAI_API_KEY") and os.getenv("MARKET_MODEL")
                    else ConservativeMarketReasoner()
                )
                _DEFAULT_AGENT = MarketNode(reasoner)
    return _DEFAULT_AGENT


def build_market_graph(agent: MarketNode | None = None):
    return (agent or default_market_agent()).build_graph()


def run_market(worker_input: dict, *, agent: MarketNode | None = None) -> dict:
    return (agent or default_market_agent())(worker_input)


def market_agent_node(worker_input: dict) -> dict:
    return default_market_agent()(worker_input)
