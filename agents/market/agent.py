"""LangGraph subgraph for Healthcare AI market and business analysis."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from datetime import date, datetime
from typing import Annotated, Any, TypedDict
from urllib.parse import urlparse

from langgraph.graph import END, START, StateGraph

from rag.market.retriever import EmptyRetriever, RetrievedChunk, Retriever

from .config import MarketConfig
from .providers import (
    ConservativeMarketReasoner,
    DisabledWebSearchProvider,
    MarketReasoner,
    OpenAIStructuredMarketReasoner,
    TavilySearchProvider,
    WebSearchProvider,
)
from .schema import (
    CRITERIA_VERSION,
    DIMENSIONS,
    SCHEMA_VERSION,
    AnalysisEnvelope,
    AnalysisStatus,
    AssessmentCheck,
    CheckStatus,
    DimensionDraft,
    DimensionResult,
    Evidence,
    EvidenceStatus,
    Finding,
    MarketAgentOutput,
    MarketData,
    MarketDimensions,
    MarketQueries,
    MarketScope,
    MissingCause,
    MissingItem,
    QuestionResult,
    RetrievalLog,
    ReviewRequest,
    ReviewResponse,
    ScoreStatus,
    SearchStatus,
    Source,
    SourceType,
    SupplementalContext,
    SupplementalFact,
    WebSearchLog,
)
from .scoring import (
    RUBRICS,
    comparable_cagr_value,
    criteria_inputs,
    display_overall_score,
    score_dimension,
)

RESULT_KEYS = {
    "size_growth": "size_growth_result",
    "demand": "demand_result",
    "commercialization": "commercialization_result",
    "monetization": "monetization_result",
}

_DEFAULT_AGENT: MarketAgent | None = None
_DEFAULT_AGENT_LOCK = threading.Lock()

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

WEB_TERMS = {
    "product_type": "제품 서비스 핵심 기능 시장 분류",
    "intended_use": "제품 사용 목적 intended use",
    "target_customer": "목표 고객 customer",
    "buyer": "구매자 지불 주체 payer",
    "primary_user": "실제 사용자 workflow",
    "business_model": "사업모델 가격 과금 라이선스",
    "geography": "진출 국가 지역 해외 시장",
    "core_problem": "해결 문제 미충족 수요",
}


def _merge_unique(
    left: list[dict] | None, right: list[dict] | None, key: str
) -> list[dict]:
    result = {item[key]: item for item in left or []}
    for item in right or []:
        identity = item[key]
        if identity in result and result[identity] != item:
            # A later review may enrich the same stable object.  Preserve the
            # new representation while still preventing duplicate IDs.
            result[identity] = item
        else:
            result[identity] = item
    return list(result.values())


def merge_sources(left, right):
    return _merge_unique(left, right, "source_id")


def merge_evidence(left, right):
    return _merge_unique(left, right, "evidence_id")


def merge_missing(left, right):
    return _merge_unique(left, right, "item_id")


def merge_logs(left, right):
    return list(left or []) + list(right or [])


class MarketState(TypedDict, total=False):
    company_id: str
    as_of: str
    run_id: str
    schema_version: str
    criteria_version: str
    company_profile: dict
    required_context: list[str]
    supplemental_context: dict
    market_scope: dict
    market_queries: dict
    size_growth_result: dict
    demand_result: dict
    commercialization_result: dict
    monetization_result: dict
    sources: Annotated[list[dict], merge_sources]
    evidence: Annotated[list[dict], merge_evidence]
    missing_items: Annotated[list[dict], merge_missing]
    retrieval_log: Annotated[list[dict], merge_logs]
    web_search_log: Annotated[list[dict], merge_logs]
    errors: Annotated[list[dict], merge_logs]
    review_request: dict | None
    review_round: int
    previous_analysis: dict | None
    review_response: dict | None
    market_analysis: dict


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _hash(prefix: str, value: str, length: int = 16) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


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


def _company_name(profile: dict[str, Any]) -> str:
    for key in ("company_name", "legal_name", "name", "기업명"):
        if profile.get(key):
            return str(profile[key])
    raise ValueError("company_profile requires a company name")


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


def _source_type(url: str, publisher: str = "") -> SourceType:
    host = urlparse(url).netloc.lower()
    if host.endswith((".go.kr", ".gov", ".gov.uk")):
        return SourceType.OFFICIAL
    if "doi.org" in host or "pubmed" in host or "journal" in host:
        return SourceType.RESEARCH
    if publisher and publisher.lower() in host:
        return SourceType.COMPANY
    return SourceType.OTHER


def _safe_source_type(value: Any) -> SourceType:
    try:
        return SourceType(str(value or "other"))
    except ValueError:
        return SourceType.OTHER


def _supplemental_values(context: SupplementalContext) -> dict[str, Any]:
    return {fact.field: fact.value for fact in context.facts}


def _merge_supplemental(
    previous: SupplementalContext, current: SupplementalContext
) -> SupplementalContext:
    facts = {fact.field: fact for fact in previous.facts}
    facts.update({fact.field: fact for fact in current.facts})
    unresolved = (
        set(previous.unresolved_fields) | set(current.unresolved_fields)
    ) - set(facts)
    return SupplementalContext(
        facts=list(facts.values()), unresolved_fields=sorted(unresolved)
    )


def _query_set(
    scope: MarketScope, review: ReviewRequest | None = None
) -> MarketQueries:
    segment = scope.segment
    geographies = " ".join(scope.geography) or "global"
    customer = scope.target_customer
    buyer = scope.buyer
    period = scope.forecast_period
    queries = MarketQueries(
        size_growth=[
            f"{segment} market size CAGR {geographies} {period}",
            f"{segment} industry forecast {geographies} base year {scope.base_year}",
        ],
        demand=[
            f"{customer} unmet need demand {segment}",
            f"{buyer} adoption intention budget willingness to pay {segment}",
        ],
        commercialization=[
            f"{segment} implementation workflow integration infrastructure requirements",
            f"{buyer} procurement deployment barriers switching cost {segment}",
        ],
        monetization=[
            f"{segment} pricing revenue model subscription license per-use",
            f"{buyer} procurement payment pathway economics ROI {segment}",
        ],
    )
    if review:
        extra = [question.text for question in review.questions]
        for dimension in review_dimensions(review):
            values = getattr(queries, dimension)
            setattr(queries, dimension, list(dict.fromkeys(extra + values)))
    return queries


def review_dimensions(request: ReviewRequest) -> list[str]:
    if request.dimensions:
        return list(dict.fromkeys(request.dimensions))
    criterion = request.criterion_id.upper()
    if criterion == "C2":
        return ["size_growth"]
    if criterion == "C3":
        return ["demand", "commercialization"]
    if criterion == "C4":
        return ["monetization"]
    joined = " ".join(
        request.finding_ids + [question.text for question in request.questions]
    ).lower()
    mapping = {
        "size_growth": ("size", "growth", "시장규모", "성장", "cagr"),
        "demand": ("demand", "수요", "고객"),
        "commercialization": ("commercial", "adoption", "상용화", "도입"),
        "monetization": ("monet", "revenue model", "수익화", "과금"),
    }
    found = [
        dimension
        for dimension, words in mapping.items()
        if any(word in joined for word in words)
    ]
    return found or list(DIMENSIONS)


def _review_route(request: ReviewRequest | None) -> str:
    if not request:
        return "auto"
    if request.source_route != "auto":
        return request.source_route
    text = " ".join(
        [request.reason, request.blocker] + [q.text for q in request.questions]
    ).lower()
    company_terms = (
        "제품",
        "고객",
        "구매자",
        "사업모델",
        "가격",
        "진출",
        "회사",
        "buyer",
        "customer",
    )
    market_terms = ("시장규모", "cagr", "산업", "도입률", "조달", "시장 근거", "market")
    web = any(term in text for term in company_terms)
    rag = any(term in text for term in market_terms)
    return "both" if web and rag else "web" if web else "rag"


class MarketAgent:
    def __init__(
        self,
        search: WebSearchProvider,
        retriever: Retriever,
        reasoner: MarketReasoner,
        *,
        config: MarketConfig | None = None,
    ):
        self.search = search
        self.retriever = retriever
        self.reasoner = reasoner
        self.config = config or MarketConfig.from_env()

    def _check_input(self, state: MarketState) -> dict:
        request = (
            ReviewRequest.model_validate(state["review_request"])
            if state.get("review_request")
            else None
        )
        route = _review_route(request)
        if request and route not in ("web", "both"):
            missing: list[str] = []
        else:
            missing = required_market_context(state["company_profile"])
            if request and route in ("web", "both") and not missing:
                # Review questions may request a fresh company-specific fact even
                # when the original profile was structurally complete.
                question_text = " ".join(q.text for q in request.questions).lower()
                missing = [
                    field
                    for field, terms in WEB_TERMS.items()
                    if any(t in question_text for t in terms.split())
                ]
        return {"required_context": list(dict.fromkeys(missing))}

    @staticmethod
    def _after_input(state: MarketState) -> str:
        return "supplement" if state.get("required_context") else "scope"

    def _supplement(self, state: MarketState) -> dict:
        profile = state["company_profile"]
        company_name = _company_name(profile)
        product_name = str(
            _product(profile).get("name")
            or _product(profile).get("product_name")
            or profile.get("product_name")
            or ""
        )
        checked_at = _today()
        source_models: dict[str, Source] = {}
        source_contents: dict[str, str] = {}
        logs: list[dict] = []
        errors: list[dict] = []
        for field in state.get("required_context", [])[: self.config.max_web_queries]:
            query = (
                f'"{company_name}" {product_name} {WEB_TERMS.get(field, field)}'.strip()
            )
            try:
                results = self.search.search(
                    query,
                    as_of=state["as_of"],
                    max_results=self.config.web_results_per_query,
                )
            except Exception as exc:  # noqa: BLE001 - isolate an external search failure per field
                errors.append(
                    {"stage": "web_search", "field": field, "error": type(exc).__name__}
                )
                logs.append(
                    WebSearchLog(
                        query=query,
                        purpose=field,
                        searched_at=_now(),
                        checked_at=checked_at,
                        status=SearchStatus.ERROR,
                        error="web search failed",
                    ).model_dump(mode="json")
                )
                continue
            if not results:
                logs.append(
                    WebSearchLog(
                        query=query,
                        purpose=field,
                        searched_at=_now(),
                        checked_at=checked_at,
                        status=SearchStatus.NOT_FOUND,
                    ).model_dump(mode="json")
                )
            for result in results:
                sid = _hash(f"{state['company_id']}:market:web:src-", result.url)
                source_models[sid] = Source(
                    source_id=sid,
                    title=result.title,
                    url=result.url,
                    publisher=result.publisher,
                    source_type=_source_type(result.url),
                    published_at=result.published_at,
                    checked_at=checked_at,
                )
                source_contents[sid] = result.content
                logs.append(
                    WebSearchLog(
                        query=query,
                        purpose=field,
                        searched_at=_now(),
                        checked_at=checked_at,
                        status=SearchStatus.FOUND,
                        source_url=result.url,
                        source_title=result.title,
                        publisher=result.publisher,
                        source_id=sid,
                    ).model_dump(mode="json")
                )

        source_payload = [
            {
                **source.model_dump(mode="json"),
                "content": source_contents[source.source_id],
            }
            for source in source_models.values()
        ]
        try:
            draft = self.reasoner.supplement(
                profile, state.get("required_context", []), source_payload
            )
        except Exception as exc:  # noqa: BLE001 - fall back without losing collected sources
            errors.append(
                {"stage": "supplement_reasoning", "error": type(exc).__name__}
            )
            draft = ConservativeMarketReasoner().supplement(
                profile, state.get("required_context", []), source_payload
            )

        evidence: list[dict] = []
        facts: list[SupplementalFact] = []
        for fact in draft.facts:
            valid_ids = [
                source_id
                for source_id in fact.source_ids
                if source_id in source_contents
            ]
            exact = bool(fact.excerpt) and any(
                fact.excerpt in source_contents[source_id] for source_id in valid_ids
            )
            status = (
                EvidenceStatus.CONFIRMED
                if exact
                else EvidenceStatus.PARTIAL
                if valid_ids
                else EvidenceStatus.UNVERIFIED
            )
            normalized = fact.model_copy(
                update={"source_ids": valid_ids, "evidence_status": status}
            )
            facts.append(normalized)
            if valid_ids:
                evidence_id = _hash(
                    f"{state['company_id']}:market:web:ev-",
                    f"{fact.field}|{fact.value}|{'|'.join(valid_ids)}",
                )
                evidence.append(
                    Evidence(
                        evidence_id=evidence_id,
                        company_id=state["company_id"],
                        statement=f"{fact.field}: {fact.value}",
                        source_ids=valid_ids,
                        locator=source_models[valid_ids[0]].url,
                        excerpt=fact.excerpt if exact else None,
                        evidence_status=status,
                        dimension="market_scope",
                    ).model_dump(mode="json")
                )
                for log in logs:
                    if log.get("source_id") in valid_ids and not log.get(
                        "extracted_fact"
                    ):
                        log["extracted_fact"] = f"{fact.field}: {fact.value}"

        previous = SupplementalContext.model_validate(
            state.get("supplemental_context") or {}
        )
        current = SupplementalContext(
            facts=facts, unresolved_fields=draft.unresolved_fields
        )
        supplemental = _merge_supplemental(previous, current)
        missing: list[dict] = []
        for field in supplemental.unresolved_fields:
            item_id = _hash(
                f"{state['company_id']}:market:missing-", f"web|{field}", 12
            )
            missing.append(
                MissingItem(
                    item_id=item_id,
                    field=field,
                    cause=MissingCause.NOT_FOUND
                    if source_models
                    else MissingCause.SEARCH_FAILED,
                    route="web",
                    impact="시장 범위 정의의 불확실성 증가",
                    detail=f"외부 검색 후에도 {field} 확인 불가",
                    dimension="market_scope",
                ).model_dump(mode="json")
            )
        return {
            "supplemental_context": supplemental.model_dump(mode="json"),
            "sources": [
                source.model_dump(mode="json") for source in source_models.values()
            ],
            "evidence": evidence,
            "missing_items": missing,
            "web_search_log": logs,
            "errors": errors,
        }

    def _scope(self, state: MarketState) -> dict:
        request = (
            ReviewRequest.model_validate(state["review_request"])
            if state.get("review_request")
            else None
        )
        if request and _review_route(request) == "rag" and state.get("market_scope"):
            return {}
        try:
            scope = self.reasoner.define_scope(
                state["company_profile"],
                state.get("supplemental_context") or {},
                as_of=state["as_of"],
            )
            return {"market_scope": scope.model_dump(mode="json")}
        except Exception as exc:  # noqa: BLE001 - conservative scope keeps the graph executable
            fallback = ConservativeMarketReasoner().define_scope(
                state["company_profile"],
                state.get("supplemental_context") or {},
                as_of=state["as_of"],
            )
            return {
                "market_scope": fallback.model_dump(mode="json"),
                "errors": [{"stage": "market_scope", "error": type(exc).__name__}],
            }

    def _queries(self, state: MarketState) -> dict:
        scope = MarketScope.model_validate(state["market_scope"])
        request = (
            ReviewRequest.model_validate(state["review_request"])
            if state.get("review_request")
            else None
        )
        return {"market_queries": _query_set(scope, request).model_dump(mode="json")}

    def _source_from_chunk(self, chunk: RetrievedChunk) -> Source:
        meta = chunk.metadata
        document_id = str(
            meta.get("document_id")
            or _hash("market-doc-", str(meta.get("path") or chunk.chunk_id))
        )
        source_id = _hash("market:rag:src-", document_id)
        return Source(
            source_id=source_id,
            title=str(meta.get("title") or meta.get("path") or document_id),
            url=str(meta.get("source_url") or meta.get("path") or document_id),
            publisher=str(meta.get("publisher") or "unknown"),
            source_type=_safe_source_type(meta.get("source_type")),
            published_at=str(meta.get("published_at") or "") or None,
            checked_at=_today(),
            document_id=document_id,
        )

    def _dimension_node(self, dimension: str):
        result_key = RESULT_KEYS[dimension]

        def node(state: MarketState) -> dict:
            queries = list((state.get("market_queries") or {}).get(dimension) or [])
            sources: dict[str, Source] = {}
            chunks: dict[str, RetrievedChunk] = {}
            logs: list[dict] = []
            errors: list[dict] = []
            for query in queries:
                try:
                    found = self.retriever.retrieve(
                        query, top_k=self.config.retrieval_top_k
                    )
                    valid: list[RetrievedChunk] = []
                    for chunk in found:
                        published = str(chunk.metadata.get("published_at") or "")
                        if published and published[:10] > state["as_of"]:
                            continue
                        valid.append(chunk)
                        previous = chunks.get(chunk.chunk_id)
                        if previous is None or chunk.score > previous.score:
                            chunks[chunk.chunk_id] = chunk
                        source = self._source_from_chunk(chunk)
                        sources[source.source_id] = source
                    logs.append(
                        RetrievalLog(
                            query=query,
                            dimension=dimension,
                            searched_at=_now(),
                            status=SearchStatus.FOUND
                            if valid
                            else SearchStatus.NOT_FOUND,
                            source_ids=sorted(
                                {
                                    self._source_from_chunk(item).source_id
                                    for item in valid
                                }
                            ),
                            chunk_ids=[item.chunk_id for item in valid],
                            top_k=self.config.retrieval_top_k,
                        ).model_dump(mode="json")
                    )
                except Exception as exc:  # noqa: BLE001 - one failed query must not cancel fan-out
                    errors.append(
                        {
                            "stage": "retrieval",
                            "dimension": dimension,
                            "error": type(exc).__name__,
                        }
                    )
                    logs.append(
                        RetrievalLog(
                            query=query,
                            dimension=dimension,
                            searched_at=_now(),
                            status=SearchStatus.ERROR,
                            top_k=self.config.retrieval_top_k,
                            error="dense retrieval failed",
                        ).model_dump(mode="json")
                    )

            ranked = sorted(chunks.values(), key=lambda item: item.score, reverse=True)[
                : self.config.max_retrieved_chunks_per_dimension
            ]
            evidence_models: list[Evidence] = []
            evidence_context: list[dict] = []
            for chunk in ranked:
                source = self._source_from_chunk(chunk)
                page_raw = chunk.metadata.get("page")
                page = (
                    int(page_raw)
                    if str(page_raw).isdigit() and int(page_raw) > 0
                    else None
                )
                evidence_id = _hash(
                    f"{state['company_id']}:market:{dimension}:ev-", chunk.chunk_id, 18
                )
                excerpt = chunk.text[:4000]
                evidence = Evidence(
                    evidence_id=evidence_id,
                    company_id=state["company_id"],
                    statement=f"{source.title}의 {dimension} 관련 원문 근거",
                    source_ids=[source.source_id],
                    locator=f"page {page}"
                    if page
                    else str(chunk.metadata.get("path") or ""),
                    page=page,
                    excerpt=excerpt,
                    evidence_status=EvidenceStatus.CONFIRMED,
                    dimension=dimension,
                )
                evidence_models.append(evidence)
                evidence_context.append(
                    {
                        **evidence.model_dump(mode="json"),
                        "retrieval_score": chunk.score,
                        "document_title": source.title,
                        "publisher": source.publisher,
                        "published_at": source.published_at,
                        "geography": chunk.metadata.get("region"),
                        "segment": chunk.metadata.get("segment"),
                    }
                )

            missing_models: list[MissingItem] = []
            request = (
                ReviewRequest.model_validate(state["review_request"])
                if state.get("review_request")
                else None
            )
            if not evidence_context:
                draft = DimensionDraft(
                    rationale="RAG에서 해당 평가영역의 근거를 찾지 못해 판단 불가",
                    missing_fields=[f"{dimension}_evidence"],
                )
            else:
                try:
                    draft = self.reasoner.analyze_dimension(
                        dimension,
                        state["market_scope"],
                        state["company_profile"],
                        state.get("supplemental_context") or {},
                        evidence_context,
                        RUBRICS[dimension],
                        request.model_dump(mode="json") if request else None,
                    )
                except Exception as exc:  # noqa: BLE001 - preserve retrieval and return unknown
                    errors.append(
                        {
                            "stage": "dimension_reasoning",
                            "dimension": dimension,
                            "error": type(exc).__name__,
                        }
                    )
                    draft = ConservativeMarketReasoner().analyze_dimension(
                        dimension,
                        state["market_scope"],
                        state["company_profile"],
                        state.get("supplemental_context") or {},
                        evidence_context,
                        RUBRICS[dimension],
                        request.model_dump(mode="json") if request else None,
                    )

            allowed_evidence = {item.evidence_id for item in evidence_models}
            draft.evidence_ids = [
                item for item in draft.evidence_ids if item in allowed_evidence
            ]
            normalized_checks: list[AssessmentCheck] = []
            for check in draft.checks:
                evidence_ids = [
                    item for item in check.evidence_ids if item in allowed_evidence
                ]
                if (
                    check.status in (CheckStatus.YES, CheckStatus.NO)
                    and not evidence_ids
                ):
                    normalized_checks.append(
                        AssessmentCheck(
                            check_id=check.check_id,
                            status=CheckStatus.UNKNOWN,
                            rationale="검색된 원문 근거 ID로 확인하지 못함",
                        )
                    )
                else:
                    normalized_checks.append(
                        AssessmentCheck(
                            check_id=check.check_id,
                            status=check.status,
                            rationale=check.rationale,
                            evidence_ids=evidence_ids,
                        )
                    )
            draft.checks = normalized_checks
            for metric in draft.market_metrics:
                metric.evidence_ids = [
                    item for item in metric.evidence_ids if item in allowed_evidence
                ]
            draft.market_metrics = [
                metric for metric in draft.market_metrics if metric.evidence_ids
            ]
            scope = MarketScope.model_validate(state["market_scope"])
            if (
                dimension == "size_growth"
                and comparable_cagr_value(draft, scope) is None
            ):
                draft.missing_fields = list(
                    dict.fromkeys(draft.missing_fields + ["comparable_cagr"])
                )
            for field in draft.missing_fields:
                item_id = _hash(
                    f"{state['company_id']}:market:missing-", f"{dimension}|{field}", 12
                )
                missing_models.append(
                    MissingItem(
                        item_id=item_id,
                        field=field,
                        cause=MissingCause.RETRIEVAL_EMPTY
                        if not evidence_context
                        else MissingCause.NOT_FOUND,
                        route="rag",
                        impact=f"{dimension} 점수 판단 제한",
                        dimension=dimension,
                    )
                )
            result = score_dimension(
                dimension,
                draft,
                [item.item_id for item in missing_models],
                scope=scope,
            )
            return {
                result_key: result.model_dump(mode="json"),
                "sources": [
                    source.model_dump(mode="json") for source in sources.values()
                ],
                "evidence": [item.model_dump(mode="json") for item in evidence_models],
                "missing_items": [
                    item.model_dump(mode="json") for item in missing_models
                ],
                "retrieval_log": logs,
                "errors": errors,
            }

        return node

    def _merge(self, state: MarketState) -> dict:
        dimension_values = {
            dimension: DimensionResult.model_validate(state[RESULT_KEYS[dimension]])
            for dimension in DIMENSIONS
        }
        dimensions = MarketDimensions(**dimension_values)
        metrics = []
        seen_metrics = set()
        concerns: list[str] = []
        for result in dimension_values.values():
            for metric in result.market_metrics:
                if metric.metric_id not in seen_metrics:
                    metrics.append(metric)
                    seen_metrics.add(metric.metric_id)
            concerns.extend(result.business_concerns)
        criteria = criteria_inputs(dimensions)
        overall = display_overall_score(dimensions)
        summary_parts = [
            f"{dimension}={result.score if result.score_status == ScoreStatus.SCORED else result.score_status.value}"
            for dimension, result in dimension_values.items()
        ]
        summary = "시장·사업성 평가: " + ", ".join(summary_parts)

        findings = []
        criterion = {
            "size_growth": ["C2"],
            "demand": ["C3"],
            "commercialization": ["C3"],
            "monetization": ["C4"],
        }
        for dimension, result in dimension_values.items():
            findings.append(
                Finding(
                    finding_id=f"{state['company_id']}:market:f-{dimension}",
                    category=dimension,
                    claim=f"{dimension}: {result.rationale}",
                    evidence_ids=result.evidence_ids,
                    uncertainty=(
                        "공개 근거 부족으로 판단 불가"
                        if result.score_status == ScoreStatus.UNKNOWN
                        else None
                    ),
                    related_criterion_ids=criterion[dimension],
                )
            )

        previous = state.get("previous_analysis") or {}
        result_version = int(previous.get("result_version") or 0) + 1
        response = (
            self._review_response(state, findings, result_version)
            if state.get("review_request")
            else None
        )
        review_responses = list(previous.get("review_responses") or [])
        if response:
            review_responses.append(response.model_dump(mode="json"))
        status = (
            AnalysisStatus.PARTIAL if state.get("errors") else AnalysisStatus.COMPLETE
        )
        data = MarketData(
            market_scope=MarketScope.model_validate(state["market_scope"]),
            supplemental_context=SupplementalContext.model_validate(
                state.get("supplemental_context") or {}
            ),
            market_metrics=metrics,
            dimensions=dimensions,
            criteria_inputs=criteria,
            overall_market_score=overall,
            market_summary=summary,
            business_concerns=list(dict.fromkeys(concerns)),
        )
        envelope = AnalysisEnvelope(
            run_id=previous.get("run_id") or state["run_id"],
            company_id=state["company_id"],
            as_of=state["as_of"],
            schema_version=state["schema_version"],
            criteria_version=state["criteria_version"],
            result_version=result_version,
            analysis_status=status,
            data=data,
            sources=[
                Source.model_validate(item) for item in state.get("sources") or []
            ],
            evidence=[
                Evidence.model_validate(item) for item in state.get("evidence") or []
            ],
            findings=findings,
            missing_items=[
                MissingItem.model_validate(item)
                for item in state.get("missing_items") or []
            ],
            retrieval_log=[
                RetrievalLog.model_validate(item)
                for item in state.get("retrieval_log") or []
            ],
            web_search_log=[
                WebSearchLog.model_validate(item)
                for item in state.get("web_search_log") or []
            ],
            review_responses=[
                ReviewResponse.model_validate(item) for item in review_responses
            ],
        )
        return {
            "market_analysis": envelope.model_dump(mode="json"),
            "review_response": response.model_dump(mode="json") if response else None,
        }

    def _review_response(
        self, state: MarketState, findings: list[Finding], result_version: int
    ) -> ReviewResponse:
        request = ReviewRequest.model_validate(state["review_request"])
        previous = state.get("previous_analysis") or {}
        before = {item["finding_id"]: item for item in previous.get("findings") or []}
        after = {item.finding_id: item.model_dump(mode="json") for item in findings}
        updated = [
            finding_id
            for finding_id, item in after.items()
            if before.get(finding_id) != item
        ]
        old_sources = {item["source_id"] for item in previous.get("sources") or []}
        new_sources = sorted(
            {item["source_id"] for item in state.get("sources") or []} - old_sources
        )
        target_results = [
            DimensionResult.model_validate(state[RESULT_KEYS[dimension]])
            for dimension in review_dimensions(request)
        ]
        fully_answered = all(
            result.score_status != ScoreStatus.UNKNOWN for result in target_results
        )
        evidence_ids = sorted(
            {eid for result in target_results for eid in result.evidence_ids}
        )
        question_results = [
            QuestionResult(
                question_id=question.question_id,
                status="answered"
                if fully_answered
                else "partially_answered"
                if evidence_ids
                else "unanswered",
                answer=(
                    "요청 영역을 재분석해 판단을 갱신함"
                    if fully_answered
                    else "추가 근거를 검토했으나 일부 판단은 unknown으로 유지"
                    if evidence_ids
                    else "추가 근거를 확보하지 못해 unknown을 유지"
                ),
                evidence_ids=evidence_ids,
            )
            for question in request.questions
        ]
        if fully_answered:
            resolution = "resolved"
        elif evidence_ids:
            resolution = "partially_resolved"
        elif any(
            log.get("status") == "error" for log in state.get("retrieval_log") or []
        ):
            resolution = "search_failed"
        else:
            resolution = "unresolved"
        return ReviewResponse(
            response_id=f"{request.request_id}:resp",
            request_id=request.request_id,
            company_id=request.company_id,
            result_version=result_version,
            resolution=resolution,
            updated_finding_ids=updated,
            new_source_ids=new_sources,
            question_results=question_results,
            remaining_unknowns=[
                dimension
                for dimension, result in zip(review_dimensions(request), target_results)
                if result.score_status == ScoreStatus.UNKNOWN
            ],
            limitations=[
                "보완은 Graph 수준 최대 1회",
                "검색 미발견은 부정 사실의 증거가 아님",
            ],
        )

    def build_graph(self, target_dimensions: list[str] | None = None):
        dimensions = target_dimensions or list(DIMENSIONS)
        graph = StateGraph(MarketState)
        graph.add_node("input_check", self._check_input)
        graph.add_node("supplement", self._supplement)
        graph.add_node("scope", self._scope)
        graph.add_node("query_generation", self._queries)
        node_names = []
        for dimension in dimensions:
            node_name = f"analyze_{dimension}"
            node_names.append(node_name)
            graph.add_node(node_name, self._dimension_node(dimension))
        graph.add_node("merge", self._merge)
        graph.add_edge(START, "input_check")
        graph.add_conditional_edges(
            "input_check",
            self._after_input,
            {"supplement": "supplement", "scope": "scope"},
        )
        graph.add_edge("supplement", "scope")
        graph.add_edge("scope", "query_generation")
        for node_name in node_names:
            graph.add_edge("query_generation", node_name)
        graph.add_edge(node_names, "merge")
        graph.add_edge("merge", END)
        return graph.compile()

    def run(
        self,
        company_profile: dict[str, Any],
        *,
        as_of: str | None = None,
        run_id: str | None = None,
        review_request: dict[str, Any] | ReviewRequest | None = None,
        previous: dict[str, Any] | AnalysisEnvelope | None = None,
    ) -> MarketAgentOutput:
        company_id = str(company_profile.get("company_id") or "")
        if not company_id:
            raise ValueError("company_profile requires company_id")
        _company_name(company_profile)
        as_of = as_of or _today()
        date.fromisoformat(as_of)
        previous_dict = (
            previous.model_dump(mode="json")
            if isinstance(previous, AnalysisEnvelope)
            else dict(previous or {})
        )
        request = (
            ReviewRequest.model_validate(review_request) if review_request else None
        )
        if request:
            if not previous_dict:
                raise ValueError("review requires previous market_analysis")
            if (
                request.company_id != company_id
                or previous_dict.get("company_id") != company_id
            ):
                raise ValueError("review company_id mismatch")
            if request.review_round > self.config.max_review_rounds:
                raise ValueError("market review limit exceeded")
            if any(
                response.get("request_id") == request.request_id
                for response in previous_dict.get("review_responses") or []
            ):
                raise ValueError("review request already processed")
            targets = review_dimensions(request)
        else:
            targets = list(DIMENSIONS)

        previous_missing = list(previous_dict.get("missing_items", []))
        if request:
            cleared_dimensions = set(targets)
            if _review_route(request) in ("web", "both"):
                cleared_dimensions.add("market_scope")
            previous_missing = [
                item
                for item in previous_missing
                if item.get("dimension") not in cleared_dimensions
            ]

        initial: MarketState = {
            "company_id": company_id,
            "as_of": as_of,
            "run_id": run_id or uuid.uuid4().hex[:12],
            "schema_version": str(
                company_profile.get("schema_version") or SCHEMA_VERSION
            ),
            "criteria_version": str(
                company_profile.get("criteria_version") or CRITERIA_VERSION
            ),
            "company_profile": company_profile,
            "required_context": [],
            "supplemental_context": previous_dict.get("data", {}).get(
                "supplemental_context", {}
            ),
            "market_scope": previous_dict.get("data", {}).get("market_scope", {}),
            "sources": previous_dict.get("sources", []),
            "evidence": previous_dict.get("evidence", []),
            "missing_items": previous_missing,
            "retrieval_log": previous_dict.get("retrieval_log", []),
            "web_search_log": previous_dict.get("web_search_log", []),
            "errors": [],
            "review_request": request.model_dump(mode="json") if request else None,
            "review_round": request.review_round if request else 0,
            "previous_analysis": previous_dict or None,
        }
        previous_dimensions = previous_dict.get("data", {}).get("dimensions", {})
        for dimension in DIMENSIONS:
            key = RESULT_KEYS[dimension]
            if dimension in previous_dimensions:
                initial[key] = previous_dimensions[dimension]
            elif dimension not in targets:
                raise ValueError(
                    f"previous analysis missing preserved dimension: {dimension}"
                )
        final = self.build_graph(targets).invoke(initial)
        envelope = AnalysisEnvelope.model_validate(final["market_analysis"])
        response = (
            ReviewResponse.model_validate(final["review_response"])
            if final.get("review_response")
            else None
        )
        return MarketAgentOutput(market_analysis=envelope, review_response=response)

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        profile = state.get("company_profile") or state.get("current_company") or {}
        previous = state.get("market_analysis")
        request = _market_review_request(
            state.get("review_requests"), str(profile.get("company_id") or "")
        )
        if request is None and previous:
            return {}
        if request:
            if int(request.get("review_round", 1)) > self.config.max_review_rounds:
                return {}
            done = {
                item.get("request_id")
                for item in (previous or {}).get("review_responses", [])
            }
            if request.get("request_id") in done:
                return {}
        output = self.run(
            profile,
            as_of=state.get("as_of"),
            run_id=state.get("run_id"),
            review_request=request,
            previous=previous,
        )
        return {"market_analysis": output.market_analysis.model_dump(mode="json")}


def _market_review_request(value: Any, company_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        candidate = value.get("market") if "market" in value else value
        if (
            isinstance(candidate, dict)
            and candidate.get("target_agent", "market") == "market"
        ):
            return candidate
        return None
    if isinstance(value, list):
        return next(
            (
                item
                for item in value
                if isinstance(item, dict)
                and item.get("target_agent") == "market"
                and item.get("company_id") == company_id
            ),
            None,
        )
    return None


def default_market_agent() -> MarketAgent:
    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is not None:
        return _DEFAULT_AGENT
    with _DEFAULT_AGENT_LOCK:
        if _DEFAULT_AGENT is not None:
            return _DEFAULT_AGENT
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        config = MarketConfig.from_env()
        search: WebSearchProvider
        reasoner: MarketReasoner
        try:
            search = (
                TavilySearchProvider()
                if os.getenv("TAVILY_API_KEY")
                else DisabledWebSearchProvider()
            )
        except Exception:  # noqa: BLE001 - optional live provider falls back to disabled search
            search = DisabledWebSearchProvider()
        try:
            reasoner = (
                OpenAIStructuredMarketReasoner()
                if os.getenv("OPENAI_API_KEY") and os.getenv("MARKET_MODEL")
                else ConservativeMarketReasoner()
            )
        except Exception:  # noqa: BLE001 - optional live provider falls back conservatively
            reasoner = ConservativeMarketReasoner()
        retriever: Retriever = EmptyRetriever()
        if config.vectorstore_dir.exists():
            try:
                from rag.market.embeddings import BgeM3Embeddings
                from rag.market.retriever import DenseRetriever
                from rag.market.vectorstore import ChromaMarketVectorStore

                retriever = DenseRetriever(
                    BgeM3Embeddings(config.embedding_model),
                    ChromaMarketVectorStore(
                        config.vectorstore_dir, config.collection_name
                    ),
                )
            except Exception:  # noqa: BLE001 - optional index degrades to empty retrieval
                retriever = EmptyRetriever()
        _DEFAULT_AGENT = MarketAgent(search, retriever, reasoner, config=config)
        return _DEFAULT_AGENT


def build_market_graph(
    agent: MarketAgent | None = None, target_dimensions: list[str] | None = None
):
    return (agent or default_market_agent()).build_graph(target_dimensions)


def run_market(
    company_profile: dict[str, Any],
    *,
    as_of: str | None = None,
    review_request: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    agent: MarketAgent | None = None,
) -> dict[str, Any]:
    output = (agent or default_market_agent()).run(
        company_profile,
        as_of=as_of,
        review_request=review_request,
        previous=previous,
    )
    return output.market_analysis.model_dump(mode="json")


def market_agent_node(state: dict[str, Any]) -> dict[str, Any]:
    return default_market_agent()(state)
