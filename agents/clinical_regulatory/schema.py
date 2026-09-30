"""
임상·인허가 분석 (AGENT 02 / clinical) - Pydantic 스키마  v2.0

구성
1) 공통 계약 (COMMON CONTRACT 2.0)
   Source · Evidence · Finding · MissingItem · Coverage
   ReviewRequest · ReviewResponse · AnalysisEnvelope
   ※ 조 공통 스키마 파일이 생기면 이 부분은 그 파일에서 import 하도록 교체한다.
2) 임상·인허가 data 객체
   ProductAssessment · RegulatoryRecord · ProgramRecord(지정·등재)
   ClinicalStudy · ClaimCheck · RedFlag · CriterionInput · ClinicalData
3) 입력 정규화 모델
4) LLM 중간 출력 모델 (내부용)
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "2.0"
CRITERIA_VERSION = "proposed-2.0"

# ══════════════════════════════════════════════
# 1) 공통 계약 enum
# ══════════════════════════════════════════════
AgentName = Literal["clinical", "market", "traction", "risk"]
AnalysisStatus = Literal["complete", "partial", "failed"]
ScoreStatus = Literal["scored", "unknown", "not_applicable"]
SourceType = Literal["official", "company", "partner", "research", "news", "other"]
EvidenceStatus = Literal["confirmed", "partial", "unverified"]
CoverageStatus = Literal[
    "reviewed",
    "insufficient_information",
    "no_relevant_evidence_found",
    "not_reviewed",
    "search_failed",
]
MissingCause = Literal["input_missing", "not_found", "search_failed", "not_reviewed"]
MissingRoute = Literal[
    "profile", "clinical", "market", "traction", "risk", "due_diligence", "none"
]
Impact = Literal["high", "medium", "low"]
CriterionId = Literal["C1", "C2", "C3", "C4", "C5", "C6"]

ReviewReason = Literal[
    "missing_evidence", "conflict", "stale_information", "interpretation_error"
]
Resolution = Literal["resolved", "partially_resolved", "unresolved", "search_failed"]
ChangeType = Literal["facts_updated", "interpretation_updated", "both", "unchanged"]
QuestionStatus = Literal["answered", "partially_answered", "unanswered"]


# ══════════════════════════════════════════════
# 1) 공통 계약 객체
# ══════════════════════════════════════════════
class Source(BaseModel):
    source_id: str
    title: str
    url: str
    publisher: str
    source_type: SourceType
    published_at: Optional[str] = None  # 발행일 미상이면 null
    checked_at: str  # 실제 확인일
    original_source_id: Optional[str] = None  # 재인용일 때 원출처


class Evidence(BaseModel):
    evidence_id: str
    company_id: str
    product_id: Optional[str] = None
    statement: str
    source_ids: list[str]
    locator: Optional[str] = None
    excerpt: Optional[str] = None
    event_date: Optional[str] = None
    geography: Optional[str] = None
    evidence_status: EvidenceStatus


class Finding(BaseModel):
    finding_id: str
    category: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: Optional[str] = None
    related_criterion_ids: list[CriterionId] = Field(default_factory=list)


class MissingItem(BaseModel):
    item_id: str
    field: str
    cause: MissingCause
    route: MissingRoute
    impact: Impact
    detail: str


class SearchScope(BaseModel):
    names: list[str] = Field(default_factory=list)  # 조회한 기업명·제품명
    queries: list[str] = Field(default_factory=list)  # 실행한 검색어
    sources: list[str] = Field(default_factory=list)  # 조회한 기관·DB
    period: Optional[str] = None


class Coverage(BaseModel):
    coverage_id: str
    area: Literal[
        "classification",
        "regulatory",
        "designation",
        "reimbursement",
        "clinical_studies",
        "claim_check",
    ]
    product_id: Optional[str] = None
    country: Optional[str] = None
    status: CoverageStatus
    search_scope: SearchScope = Field(default_factory=SearchScope)
    checked_at: str
    note: Optional[str] = None


class ReviewQuestion(BaseModel):
    question_id: str
    text: str


class SearchBudget(BaseModel):
    max_calls: int = 5


class ReviewContext(BaseModel):
    search_period: Optional[str] = None
    preferred_sources: list[str] = Field(default_factory=list)
    previous_search_history: list[Any] = Field(default_factory=list)
    related_results: list[Any] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    request_id: str
    company_id: str
    product_ids: list[str] = Field(default_factory=list)
    target_agent: AgentName
    finding_ids: list[str] = Field(default_factory=list)
    criterion_id: Optional[CriterionId] = None
    reason: ReviewReason
    blocker: bool = False
    questions: list[ReviewQuestion] = Field(default_factory=list)
    related_evidence_ids: list[str] = Field(default_factory=list)
    previous_result_version: int
    as_of: str
    review_round: int = 1
    search_budget: SearchBudget = Field(default_factory=SearchBudget)
    completion_conditions: list[str] = Field(default_factory=list)
    context: ReviewContext = Field(default_factory=ReviewContext)


class QuestionResult(BaseModel):
    question_id: str
    status: QuestionStatus
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewResponse(BaseModel):
    response_id: str
    request_id: str
    company_id: str
    result_version: int
    resolution: Resolution
    change_type: ChangeType
    updated_finding_ids: list[str] = Field(default_factory=list)
    new_source_ids: list[str] = Field(default_factory=list)
    question_results: list[QuestionResult] = Field(default_factory=list)
    remaining_unknowns: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnalysisEnvelope(BaseModel):
    run_id: str
    company_id: str
    as_of: str
    schema_version: str = SCHEMA_VERSION
    criteria_version: str = CRITERIA_VERSION
    agent: AgentName
    result_version: int
    analysis_status: AnalysisStatus
    data: dict  # 담당별 객체 (여기서는 ClinicalData)
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    review_responses: list[ReviewResponse] = Field(default_factory=list)


# ══════════════════════════════════════════════
# 2) 임상·인허가 data enum
# ══════════════════════════════════════════════
RegulatoryApplicability = Literal["in_scope", "out_of_scope", "unknown"]
ProductType = Literal[
    "ai_software", "hardware", "ivd", "research", "wellness", "service", "other"
]
ProcedureType = Literal[
    "notification",
    "certification",
    "approval",  # 국내 신고·인증·허가
    "510k",
    "de_novo",
    "pma",
    "ce_mark",
    "other",
    "unknown",
]
RecordStatus = Literal[
    "active", "pending", "expired", "revoked", "not_found", "unknown", "not_applicable"
]
EvidenceLevel = Literal[
    "L0", "L1", "L2", "L3", "L4"
]  # 내부 분류. 규제기관 공식 등급 아님
ClaimStatus = Literal["supported", "partially_supported", "contradicted", "unknown"]
ResultDirection = Literal["supports", "contradicts", "mixed", "unclear"]
RedFlagCode = Literal["CL01", "CL02", "CL03"]
RedFlagStatus = Literal["candidate", "confirmed", "resolved"]


# ══════════════════════════════════════════════
# 2) 임상·인허가 data 객체
# ══════════════════════════════════════════════
class ProductAssessment(BaseModel):
    product_id: str
    regulatory_applicability: RegulatoryApplicability
    product_type: ProductType
    assessment_status: AnalysisStatus
    evidence_level: EvidenceLevel
    clinical_score: Optional[float] = None
    score_status: ScoreStatus
    evidence_ids: list[str] = Field(default_factory=list)


class RegulatoryRecord(BaseModel):
    record_id: str
    product_id: str
    country: str
    authority: str
    procedure_type: ProcedureType
    record_number: Optional[str] = None
    permitted_use: Optional[str] = None
    status: RecordStatus
    effective_at: Optional[str] = None
    valid_until: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)


class ProgramRecord(BaseModel):
    """designation_records[] / reimbursement_records[] 공통 구조"""

    record_id: str
    product_id: str
    country: str
    program: str
    status: RecordStatus
    scope: Optional[str] = None
    effective_at: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)


class ClinicalStudy(BaseModel):
    study_id: str
    product_id: str
    design: str
    prospective: Optional[bool] = None
    multicenter: Optional[bool] = None
    external_validation: Optional[bool] = None
    sample_size: Optional[int] = None
    comparator: Optional[str] = None
    endpoint: str
    result: str
    limitations: str
    evidence_ids: list[str] = Field(default_factory=list)
    # C1 계산을 위한 확장 필드 (근거 수준과 결과 방향을 분리 기록)
    evidence_level: EvidenceLevel
    result_direction: ResultDirection
    product_match: bool  # 목표 제품·버전·용도·환자군 일치 여부
    methods_verifiable: bool  # 방법·결과를 독립적으로 확인 가능한지


class ClaimCheck(BaseModel):
    claim_id: str
    product_id: str
    original_claim: str
    status: ClaimStatus
    comparison_reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class RedFlag(BaseModel):
    flag_id: str
    finding_id: str
    code: RedFlagCode
    status: RedFlagStatus
    materiality: Impact
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProductScore(BaseModel):
    product_id: str
    score: Optional[float] = None
    score_status: ScoreStatus
    scope_weight: float = 1.0


class CriterionInput(BaseModel):
    score: Optional[float] = None
    score_status: ScoreStatus
    product_scores: list[ProductScore] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str


class ClinicalData(BaseModel):
    product_assessments: list[ProductAssessment] = Field(default_factory=list)
    regulatory_records: list[RegulatoryRecord] = Field(default_factory=list)
    designation_records: list[ProgramRecord] = Field(default_factory=list)
    reimbursement_records: list[ProgramRecord] = Field(default_factory=list)
    clinical_studies: list[ClinicalStudy] = Field(default_factory=list)
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    red_flags: list[RedFlag] = Field(default_factory=list)
    clinical_summary: str = ""
    criteria_inputs: dict[str, CriterionInput] = Field(
        default_factory=dict
    )  # {"C1": ...}
    validation_issues: list[str] = Field(default_factory=list)  # 자체 검증 결과 (확장)
    id_keys: dict[str, str] = Field(
        default_factory=dict
    )  # 보완 시 Finding·Flag ID 유지용 (내부)
    collected_profile: dict = Field(
        default_factory=dict
    )  # 자체 수집한 기업·제품 정보 (다른 담당 참고용)


# ══════════════════════════════════════════════
# 3) 입력 정규화 모델 (company_profile)
# ══════════════════════════════════════════════
class ClaimInput(BaseModel):
    claim_id: str
    text: str  # 회사 주장 원문
    claimed_at: Optional[str] = None
    url: Optional[str] = None
    locator: Optional[str] = None
    product_id: str
    country: Optional[str] = None


class ProductInput(BaseModel):
    product_id: str
    name: Optional[str] = None
    model: Optional[str] = None
    version: Optional[str] = None
    manufacturer: Optional[str] = None  # 제조/판매 주체
    intended_use: Optional[str] = None
    target_disease: Optional[str] = None
    users: Optional[str] = None
    environment: Optional[str] = None
    modality: Optional[str] = None
    target_countries: list[str] = Field(default_factory=lambda: ["KR"])
    scope_weight: float = 1.0  # 사전 고정
    claims: list[ClaimInput] = Field(default_factory=list)


class CompanyInput(BaseModel):
    company_id: str
    display_name: str
    legal_name: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    english_name: Optional[str] = None
    homepage: Optional[str] = None
    description: Optional[str] = None
    products: list[ProductInput] = Field(default_factory=list)


# ══════════════════════════════════════════════
# 4) LLM 중간 출력 (내부)
# ══════════════════════════════════════════════
class ClassifyItem(BaseModel):
    product_id: str
    product_type: ProductType
    regulatory_applicability: RegulatoryApplicability
    has_efficacy_claim: bool = Field(
        description="질병 진단·치료·예측 등 효능 주장이 있는지"
    )
    rationale: str


class ClassifyOutput(BaseModel):
    items: list[ClassifyItem]


class RegRecordDraft(BaseModel):
    product_id: str
    country: str
    authority: str
    procedure_type: ProcedureType
    record_number: Optional[str] = None
    permitted_use: Optional[str] = Field(default=None, description="사용 목적 원문")
    status: RecordStatus
    effective_at: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    valid_until: Optional[str] = None
    statement: str = Field(description="이 기록을 한 문장으로 요약한 사실")
    excerpt: Optional[str] = Field(default=None, description="원자료 발췌")
    source_ids: list[str]
    evidence_status: EvidenceStatus


class RegulatoryExtraction(BaseModel):
    records: list[RegRecordDraft] = Field(default_factory=list)


class ProgramDraft(BaseModel):
    kind: Literal["designation", "reimbursement"]
    product_id: str
    country: str
    program: str = Field(
        description="예: 혁신의료기기 지정, 혁신의료기술 평가, 비급여 등재, 급여 등재"
    )
    status: RecordStatus
    scope: Optional[str] = None
    effective_at: Optional[str] = None
    statement: str
    excerpt: Optional[str] = None
    source_ids: list[str]
    evidence_status: EvidenceStatus


class ProgramExtraction(BaseModel):
    records: list[ProgramDraft] = Field(default_factory=list)


class StudyDraft(BaseModel):
    product_id: str
    design: str
    prospective: Optional[bool] = None
    multicenter: Optional[bool] = None
    external_validation: Optional[bool] = None
    sample_size: Optional[int] = None
    comparator: Optional[str] = None
    endpoint: str
    result: str
    limitations: str
    evidence_level: EvidenceLevel
    result_direction: ResultDirection
    product_match: bool
    methods_verifiable: bool
    statement: str
    excerpt: Optional[str] = None
    event_date: Optional[str] = None
    source_ids: list[str]
    evidence_status: EvidenceStatus


class StudyExtraction(BaseModel):
    studies: list[StudyDraft] = Field(default_factory=list)


class ClaimCheckDraft(BaseModel):
    claim_id: str
    status: ClaimStatus
    comparison_reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class ClaimExtraction(BaseModel):
    checks: list[ClaimCheckDraft] = Field(default_factory=list)


class QuestionAnswerDraft(BaseModel):
    question_id: str
    status: QuestionStatus
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewAnswerOutput(BaseModel):
    answers: list[QuestionAnswerDraft] = Field(default_factory=list)
    remaining_unknowns: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


# ══════════════════════════════════════════════
# 5) 자체 수집 (CSV만 입력될 때) - LLM 중간 출력
# ══════════════════════════════════════════════
class CollectedProduct(BaseModel):
    name: str = Field(description="제품명(브랜드명)")
    model: Optional[str] = Field(default=None, description="모델명")
    intended_use: Optional[str] = None
    target_disease: Optional[str] = None
    users: Optional[str] = Field(
        default=None, description="사용자 (예: 영상의학과 전문의)"
    )
    environment: Optional[str] = Field(default=None, description="사용 환경 (예: 병원)")
    modality: Optional[str] = Field(
        default=None, description="영상·검체 종류 (예: 흉부 X-ray)"
    )


class ProfileCollection(BaseModel):
    legal_name: Optional[str] = Field(
        default=None, description="법인 정식명. 식약처 업체명이나 홈페이지 상호에서만"
    )
    english_name: Optional[str] = None
    homepage: Optional[str] = Field(
        default=None, description="공식 홈페이지 URL. 원자료에 있는 URL만"
    )
    aliases: list[str] = Field(default_factory=list)
    products: list[CollectedProduct] = Field(default_factory=list)
    extract_urls: list[str] = Field(
        default_factory=list,
        description="회사 주장 원문이 있을 페이지 URL. 원자료에 있는 URL만",
    )


class CollectedClaim(BaseModel):
    product_name: Optional[str] = Field(
        default=None, description="해당 제품명. 불명확하면 null"
    )
    text: str = Field(
        description="페이지 본문에 있는 문장을 한 글자도 바꾸지 않고 그대로"
    )
    url: str
    claimed_at: Optional[str] = None


class ClaimCollection(BaseModel):
    claims: list[CollectedClaim] = Field(default_factory=list)
