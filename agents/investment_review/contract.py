"""06 자체 출력 계약: InvestmentReview, CriterionResult, GateResult, ValidationIssue.
다른 Agent의 형식은 바꾸지 않고 adapters/에서 각자 형식 그대로 읽는다.

근거: 통합 설계서 18쪽 InvestmentReview, README 「판정·선정 이유 코드」.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .policy import CRITERIA, CRITERIA_VERSION, GATES, SCHEMA_VERSION, SCORE_MAX, SCORE_MIN


# ─────────────────────────────────────────────
# 선택지
# ─────────────────────────────────────────────
class ScoreStatus(str, Enum):
    SCORED = "scored"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class GateStatus(str, Enum):
    CONFIRMED = "confirmed"        # 모든 요건 확인 → 총점 무관 부적격
    UNRESOLVED = "unresolved"      # 구체 근거는 있으나 핵심 요건 미확인 → 판단불가
    CLEAR = "clear"                # 정해진 조사에서 차단 근거 미확인. 안전의 증명 아님
    NOT_CHECKED = "not_checked"    # 담당 분석 미실행·실패로 조사 자체를 못 함 → 판단불가


class FinalStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNDETERMINED = "undetermined"


class ReasonCode(str, Enum):
    G01 = "G01"
    G02 = "G02"
    SCORE_BELOW_60 = "SCORE_BELOW_60"
    CRITERION_BELOW_2 = "CRITERION_BELOW_2"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"


class IssueKind(str, Enum):
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICT = "conflict"
    STALE_INFORMATION = "stale_information"
    INTERPRETATION_ERROR = "interpretation_error"
    ANALYSIS_FAILED = "analysis_failed"


# ─────────────────────────────────────────────
# 기업별 판정 구성 요소
# ─────────────────────────────────────────────
class CriterionResult(BaseModel):
    """C1~C6 하나의 결과. weight·weighted_points는 scoring이 채운다."""
    criterion_id: str
    score: Optional[float] = None
    score_status: ScoreStatus = ScoreStatus.UNKNOWN
    source_agent: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    weight: Optional[float] = None
    weighted_points: Optional[float] = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.criterion_id not in CRITERIA:
            raise ValueError(f"unknown criterion_id: {self.criterion_id}")
        if self.score_status == ScoreStatus.SCORED:
            if self.score is None or not SCORE_MIN <= self.score <= SCORE_MAX:
                raise ValueError(f"{self.criterion_id}: scored requires score in [{SCORE_MIN}, {SCORE_MAX}]")
        elif self.score is not None:
            raise ValueError(f"{self.criterion_id}: {self.score_status.value} must have score=null")
        return self


class GateResult(BaseModel):
    code: str
    status: GateStatus = GateStatus.NOT_CHECKED
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def _known_gate(self):
        if self.code not in GATES:
            raise ValueError(f"unknown gate: {self.code}")
        return self


class ValidationIssue(BaseModel):
    """취합·검증에서 발견한 문제. blocking=True면 보완 후에도 남을 때 판단불가 사유가 된다."""
    issue_id: str
    kind: IssueKind
    target_agent: Optional[str] = None
    criterion_id: Optional[str] = None
    blocking: bool = False
    reviewable: bool = True          # 담당 Agent 보완으로 해결 가능한지 (ID 불일치 등은 False)
    detail: str = ""
    related_ids: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    """어댑터 출력. 각 Agent의 원래 형식에서 06이 쓰는 값만 뽑은 것. 판단(blocking 여부 등)은 하지 않는다."""
    agent: str
    company_id: Optional[str] = None
    as_of: Optional[str] = None
    analysis_status: Optional[str] = None      # 원래 값 그대로 (risk의 no_evidence 포함)
    result_version: Optional[int] = None
    evidence_ids: list[str] = Field(default_factory=list)          # 결과 안에 실제로 있는 근거 ID (risk는 passage_id)
    source_published: dict[str, Optional[str]] = Field(default_factory=dict)  # source_id → 발행일
    criteria: list[CriterionResult] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)              # 레드플래그·위험 신호·사업 우려
    due_diligence_questions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)              # 결측·미확인 (영향도는 문장에 보존)
    source_ids: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────
# 최종 결과
# ─────────────────────────────────────────────
class Selection(BaseModel):
    rank: Optional[int] = None       # 적격 기업 내 순위. 적격이 아니면 null
    selected: bool = False
    reason: str = ""


class ReportDraft(BaseModel):
    summary: str = ""
    strengths: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    due_diligence_questions: list[str] = Field(default_factory=list)


class InvestmentReview(BaseModel):
    company_id: str
    as_of: str
    schema_version: str = SCHEMA_VERSION
    criteria_version: str = CRITERIA_VERSION
    criterion_results: list[CriterionResult] = Field(default_factory=list)
    gate_results: list[GateResult] = Field(default_factory=list)
    total_score: Optional[float] = None
    final_status: FinalStatus = FinalStatus.UNDETERMINED
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    remaining_unknowns: list[str] = Field(default_factory=list)
    selection: Selection = Field(default_factory=Selection)
    review_request_ids: list[str] = Field(default_factory=list)
    review_response_ids: list[str] = Field(default_factory=list)
    report: ReportDraft = Field(default_factory=ReportDraft)
    source_ids: list[str] = Field(default_factory=list)

    def score_of(self, criterion_id: str) -> Optional[float]:
        return next((c.score for c in self.criterion_results if c.criterion_id == criterion_id), None)
