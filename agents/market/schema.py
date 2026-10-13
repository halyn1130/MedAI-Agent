"""Market data models and the common v2 worker result contract.

The market worker returns AnalysisEnvelope directly. It contains dataset_version,
coverage and errors; collection logs and scheduling limits are not worker output.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contract import WorkerResult

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


class ReviewRequest(BaseModel):
    """Review context only. Scheduling metadata is owned by the orchestrator."""

    model_config = ConfigDict(extra="allow")
    request_id: str
    company_id: str
    target_agent: Literal["market"] = "market"
    criterion_id: str | None = None
    reason: str = ""
    questions: list[ReviewQuestion] = Field(default_factory=list)


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
    resolution: Literal["resolved", "partially_resolved", "unresolved"]
    updated_finding_ids: list[str] = Field(default_factory=list)
    new_source_ids: list[str] = Field(default_factory=list)
    question_results: list[QuestionResult] = Field(default_factory=list)
    remaining_unknowns: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnalysisEnvelope(WorkerResult):
    agent: Literal["market"] = AGENT_NAME
    data: MarketData


MarketAgentOutput = AnalysisEnvelope


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
