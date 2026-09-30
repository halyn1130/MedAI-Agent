"""Risk-agent contracts. IDs are validated again against the supplied evidence."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal['external_dependency', 'key_person_continuity', 'operational_incidents']
CATEGORIES = ('external_dependency', 'key_person_continuity', 'operational_incidents')


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Source(Model):
    source_id: str
    title: str
    url: str
    publisher: str
    source_type: Literal['company_announcement', 'partner_announcement', 'official_record', 'news', 'other'] = 'other'
    published_at: date | None = None
    checked_at: date
    content: str = Field(min_length=1)


class Citation(Model):
    source_id: str
    quote: str


class Fact(Model):
    citations: list[Citation] = Field(default_factory=list)
    statement: str
    source_ids: list[str] = Field(min_length=1)


class Mitigation(Model):
    status: Literal['documented', 'unknown', 'not_applicable']
    description: str
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def documented_has_source(self):
        if self.status == 'documented' and not self.source_ids:
            raise ValueError('Documented mitigation requires a source')
        return self


class Finding(Model):
    finding_id: str
    category: Category
    title: str
    related_product: str | None = None
    facts: list[Fact] = Field(min_length=1)
    potential_impact: str
    evidence_status: Literal['confirmed', 'partial', 'unverified']
    issue_status: Literal['active', 'resolved', 'unknown', 'no_issue_evidence_found']
    mitigation: Mitigation
    unknowns: list[str] = Field(default_factory=list)
    due_diligence_questions: list[str] = Field(default_factory=list)


class Coverage(Model):
    unknowns: list[str] = Field(default_factory=list)
    due_diligence_questions: list[str] = Field(default_factory=list)
    category: Category
    status: Literal['reviewed', 'insufficient_information', 'no_relevant_evidence_found', 'not_reviewed', 'search_failed']
    finding_ids: list[str] = Field(default_factory=list)
    limitation: str | None = None


class SearchLog(Model):
    skip_reason: Literal["disabled", "budget_exhausted", "duplicate"] | None = None
    category: Category
    query: str
    searched_at: date
    status: Literal['success', 'failed', 'skipped']
    relevant_source_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class Question(Model):
    question_id: str
    question: str
    completion_condition: str


class ReviewRequest(Model):
    request_id: str
    target_agent: Literal['risk'] = 'risk'
    company_id: str
    finding_ids: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(min_length=1)
    reason: Literal['missing_information', 'conflicting_sources', 'current_status_unknown', 'interpretation_error']
    criterion_id: str
    criterion_rule: str
    decision_blocker: str
    questions: list[Question] = Field(min_length=1)
    published_after: date | None = None
    published_before: date | None = None
    preferred_sources: list[str] = Field(default_factory=list)
    attempt: int = Field(ge=1)
    max_attempts: int = Field(default=1, ge=1)
    max_search_calls: int = Field(default=3, ge=0, le=20)

    @model_validator(mode='after')
    def valid_request(self):
        if self.attempt > self.max_attempts:
            raise ValueError('Review attempt exceeds request limit')
        if len({q.question_id for q in self.questions}) != len(self.questions):
            raise ValueError('Duplicate question IDs')
        if len(set(self.categories)) != len(self.categories):
            raise ValueError('Duplicate categories')
        if self.published_after and self.published_before and self.published_after > self.published_before:
            raise ValueError('Invalid search period')
        return self


class QuestionResult(Model):
    question_id: str
    status: Literal['answered', 'partially_answered', 'unanswered']
    answer: str
    source_ids: list[str] = Field(default_factory=list)


class CategoryResult(Model):
    """LLM may return only its assigned category, not a replacement global state."""
    findings: list[Finding] = Field(default_factory=list)
    coverage: Coverage
    question_results: list[QuestionResult] = Field(default_factory=list)


class RiskAnalysis(Model):
    diagnostics: list[dict] = Field(default_factory=list)
    company_id: str
    agent: Literal['risk'] = 'risk'
    as_of: date
    analysis_status: Literal['complete', 'partial', 'failed']
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    coverage: list[Coverage]
    sources: list[Source] = Field(default_factory=list)
    search_log: list[SearchLog] = Field(default_factory=list)

    @model_validator(mode='after')
    def integrity(self):
        if sorted(c.category for c in self.coverage) != sorted(CATEGORIES):
            raise ValueError('Exactly one coverage record per category is required')
        sources = {s.source_id for s in self.sources}
        findings = {f.finding_id: f for f in self.findings}
        if len(sources) != len(self.sources) or len(findings) != len(self.findings):
            raise ValueError('Duplicate source or finding IDs')
        for f in self.findings:
            used = {i for fact in f.facts for i in fact.source_ids} | set(f.mitigation.source_ids)
            if not used <= sources:
                raise ValueError('Unknown source ID')
        for c in self.coverage:
            expected = {f.finding_id for f in self.findings if f.category == c.category}
            if set(c.finding_ids) != expected:
                raise ValueError('Coverage must reference all and only its category findings')
        for log in self.search_log:
            if not set(log.relevant_source_ids) <= sources:
                raise ValueError('Search log contains unknown source IDs')
        return self


class ReviewResponse(Model):
    request_id: str
    outcome: Literal['updated', 'no_new_evidence', 'unresolved', 'search_failed']
    updated_finding_ids: list[str]
    new_source_ids: list[str]
    change_summary: str
    question_results: list[QuestionResult]
    remaining_unknowns: list[str]
    search_limitations: list[str]


class RiskAgentOutput(Model):
    risk_analysis: RiskAnalysis
    review_response: ReviewResponse | None = None
