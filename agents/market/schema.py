"""Contracts for the market and business viability agent.

The repository does not yet expose a shared schema package.  These models keep
the README's proposed common contract and the existing traction agent envelope
shape without changing either teammate's implementation.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "2.0"
CRITERIA_VERSION = "proposed-2.0"
AGENT_NAME = "market"
DIMENSIONS = ("size_growth", "demand", "commercialization", "monetization")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ScoreStatus(str, Enum):
    SCORED = "scored"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class EvidenceStatus(str, Enum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


class CheckStatus(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class SourceType(str, Enum):
    OFFICIAL = "official"
    COMPANY = "company"
    PARTNER = "partner"
    INDUSTRY_REPORT = "industry_report"
    RESEARCH = "research"
    NEWS = "news"
    OTHER = "other"


class SearchStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"
    SKIPPED = "skipped"


class MissingCause(str, Enum):
    INPUT_MISSING = "input_missing"
    NOT_FOUND = "not_found"
    SEARCH_FAILED = "search_failed"
    RETRIEVAL_EMPTY = "retrieval_empty"
    CONFLICT = "conflict"
    NOT_REVIEWED = "not_reviewed"


class Source(Model):
    source_id: str
    title: str
    url: str
    publisher: str
    source_type: SourceType = SourceType.OTHER
    published_at: str | None = None
    checked_at: str
    document_id: str | None = None


class Evidence(Model):
    evidence_id: str
    company_id: str
    statement: str
    source_ids: list[str] = Field(default_factory=list, min_length=1)
    locator: str | None = None
    page: int | None = Field(default=None, ge=1)
    excerpt: str | None = None
    evidence_status: EvidenceStatus
    dimension: str | None = None


class Finding(Model):
    finding_id: str
    category: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: str | None = None
    related_criterion_ids: list[str] = Field(default_factory=list)


class MissingItem(Model):
    item_id: str
    field: str
    cause: MissingCause
    route: Literal["profile", "web", "rag", "due_diligence", "none"]
    impact: str
    detail: str = ""
    dimension: str | None = None


class RetrievalLog(Model):
    query: str
    dimension: str
    searched_at: str
    status: SearchStatus
    source_ids: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1)
    error: str | None = None


class WebSearchLog(Model):
    query: str
    purpose: str
    searched_at: str
    status: SearchStatus
    source_url: str | None = None
    source_title: str | None = None
    publisher: str | None = None
    checked_at: str
    extracted_fact: str | None = None
    source_id: str | None = None
    error: str | None = None


class SupplementalFact(Model):
    field: str
    value: str | list[str]
    source_ids: list[str] = Field(default_factory=list)
    excerpt: str | None = None
    evidence_status: EvidenceStatus = EvidenceStatus.UNVERIFIED


class SupplementalContext(Model):
    facts: list[SupplementalFact] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)

    def value_for(self, field: str) -> str | list[str] | None:
        for fact in self.facts:
            if fact.field == field:
                return fact.value
        return None


class MarketScope(Model):
    segment: str
    geography: list[str] = Field(default_factory=list)
    target_customer: str
    buyer: str
    primary_user: str | None = None
    product_type: str | None = None
    intended_use: str | None = None
    core_problem: str | None = None
    business_model: str | None = None
    base_year: int
    forecast_period: str
    assumptions: list[str] = Field(default_factory=list)


class MarketQueries(Model):
    size_growth: list[str] = Field(default_factory=list)
    demand: list[str] = Field(default_factory=list)
    commercialization: list[str] = Field(default_factory=list)
    monetization: list[str] = Field(default_factory=list)


class MarketMetric(Model):
    metric_id: str
    metric_type: Literal[
        "market_size", "cagr", "adoption_rate", "willingness_to_pay", "other"
    ]
    value: float
    unit: str
    currency: str | None = None
    geography: str
    segment: str
    base_year: int | None = None
    start_year: int | None = None
    end_year: int | None = None
    method: str
    assumption: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, min_length=1)


class AssessmentCheck(Model):
    check_id: str
    status: CheckStatus
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_for_known_answer(self):
        if self.status in (CheckStatus.YES, CheckStatus.NO) and not self.evidence_ids:
            raise ValueError("yes/no checks require evidence_ids")
        return self


class DimensionResult(Model):
    score: float | None = Field(default=None, ge=0, le=5)
    score_status: ScoreStatus = ScoreStatus.UNKNOWN
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    missing_item_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    checks: list[AssessmentCheck] = Field(default_factory=list)
    market_metrics: list[MarketMetric] = Field(default_factory=list)
    business_concerns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def score_matches_status(self):
        if self.score_status == ScoreStatus.SCORED and self.score is None:
            raise ValueError("scored result requires a numeric score")
        if self.score_status != ScoreStatus.SCORED and self.score is not None:
            raise ValueError("unknown/not_applicable results must not contain a score")
        return self


class MarketDimensions(Model):
    size_growth: DimensionResult
    demand: DimensionResult
    commercialization: DimensionResult
    monetization: DimensionResult


class CriterionInput(Model):
    score: float | None = Field(default=None, ge=0, le=5)
    score_status: ScoreStatus = ScoreStatus.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class MarketCriteriaInputs(Model):
    C2: CriterionInput = Field(default_factory=CriterionInput)
    C3: CriterionInput = Field(default_factory=CriterionInput)
    C4: CriterionInput = Field(default_factory=CriterionInput)


class MarketData(Model):
    market_scope: MarketScope
    supplemental_context: SupplementalContext = Field(
        default_factory=SupplementalContext
    )
    market_metrics: list[MarketMetric] = Field(default_factory=list)
    dimensions: MarketDimensions
    criteria_inputs: MarketCriteriaInputs = Field(default_factory=MarketCriteriaInputs)
    overall_market_score: float | None = Field(default=None, ge=0, le=5)
    market_summary: str
    business_concerns: list[str] = Field(default_factory=list)


class ReviewQuestion(Model):
    question_id: str
    text: str


class ReviewRequest(Model):
    request_id: str
    company_id: str
    target_agent: Literal["market"] = "market"
    finding_ids: list[str] = Field(default_factory=list)
    criterion_id: str
    reason: str
    blocker: str
    questions: list[ReviewQuestion] = Field(default_factory=list)
    related_evidence_ids: list[str] = Field(default_factory=list)
    review_round: int = Field(default=1, ge=1)
    completion_conditions: list[str] = Field(default_factory=list)
    related_results: list[str] = Field(default_factory=list)
    dimensions: list[
        Literal["size_growth", "demand", "commercialization", "monetization"]
    ] = Field(default_factory=list)
    source_route: Literal["auto", "web", "rag", "both"] = "auto"


class QuestionResult(Model):
    question_id: str
    status: Literal["answered", "partially_answered", "unanswered"]
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewResponse(Model):
    response_id: str
    request_id: str
    company_id: str
    result_version: int
    resolution: Literal["resolved", "partially_resolved", "unresolved", "search_failed"]
    updated_finding_ids: list[str] = Field(default_factory=list)
    new_source_ids: list[str] = Field(default_factory=list)
    question_results: list[QuestionResult] = Field(default_factory=list)
    remaining_unknowns: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnalysisEnvelope(Model):
    run_id: str
    company_id: str
    as_of: str
    schema_version: str = SCHEMA_VERSION
    criteria_version: str = CRITERIA_VERSION
    agent: Literal["market"] = AGENT_NAME
    result_version: int = Field(default=1, ge=1)
    analysis_status: AnalysisStatus = AnalysisStatus.COMPLETE
    data: MarketData
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    retrieval_log: list[RetrievalLog] = Field(default_factory=list)
    web_search_log: list[WebSearchLog] = Field(default_factory=list)
    review_responses: list[ReviewResponse] = Field(default_factory=list)

    @model_validator(mode="after")
    def reference_integrity(self):
        source_ids = {source.source_id for source in self.sources}
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source_id")
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("duplicate evidence_id")
        for item in self.evidence:
            if not set(item.source_ids) <= source_ids:
                raise ValueError(
                    f"evidence {item.evidence_id} references an unknown source"
                )
        for finding in self.findings:
            if not set(finding.evidence_ids) <= evidence_ids:
                raise ValueError(
                    f"finding {finding.finding_id} references unknown evidence"
                )
        return self


class MarketAgentOutput(Model):
    market_analysis: AnalysisEnvelope
    review_response: ReviewResponse | None = None


# Structured outputs used by an injected LLM reasoner.
class SupplementalDraft(Model):
    facts: list[SupplementalFact] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)


class MarketScopeDraft(MarketScope):
    pass


class DimensionDraft(Model):
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    checks: list[AssessmentCheck] = Field(default_factory=list)
    market_metrics: list[MarketMetric] = Field(default_factory=list)
    business_concerns: list[str] = Field(default_factory=list)
    not_applicable: bool = False
    missing_fields: list[str] = Field(default_factory=list)


def dump_jsonable(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
