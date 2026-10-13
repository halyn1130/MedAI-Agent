"""
임상·인허가 평가 (Worker: clinical) - Pydantic 스키마  v3.0

구성
1) 공통 계약 (공통 State 및 Worker 입출력 계약 초안)
   Source · DataRecord · Evidence · Finding · MissingItem · Coverage
   ReviewResponse · WorkerError · WorkerResult
   ※ 조 공통 스키마 파일이 생기면 이 부분은 그 파일에서 import 하도록 교체한다.
   ※ 공통 필드 외에 임상에서 쓰는 부가 필드는 각 모델에 "임상 부가"로 표시했다.
2) 임상·인허가 data 객체
   ProductAssessment · RegulatoryRecord · ProgramRecord(지정·등재)
   ClinicalStudy · ClaimCheck · RedFlag · CriterionInput · ClinicalData
3) 입력 정규화 모델
4) LLM 중간 출력 모델 (내부용)
5) 수집 단계(collection.py)용 LLM 출력 모델
6) 기존 06 심사(investment_review) 호환용 ReviewRequest
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "2.0"
CRITERIA_VERSION = "proposed-2.0"

# ══════════════════════════════════════════════
# 1) 공통 계약 enum
# ══════════════════════════════════════════════
WorkerName = Literal["clinical", "market", "traction", "risk"]
AnalysisStatus = Literal["complete", "partial", "failed"]
ScoreStatus = Literal["scored", "unknown", "not_applicable"]
EvidenceStatus = Literal["confirmed", "partial", "unverified"]
CoverageStatus = Literal["reviewed", "partial", "not_reviewed", "not_applicable"]  # reviewed = 제공 자료를 검토함
Impact = Literal["high", "medium", "low"]
CriterionId = Literal["C1", "C2", "C3", "C4", "C5", "C6"]


# ══════════════════════════════════════════════
# 1) 공통 계약 객체
# ══════════════════════════════════════════════
class Source(BaseModel):
    source_id: str
    title: str
    url: Optional[str] = None
    published_at: Optional[str] = None  # 공개일 미상이면 null
    collected_at: Optional[str] = None  # 수집 시점
    publisher: Optional[str] = None  # 임상 수집 부가
    source_type: Optional[str] = None  # 임상 수집 부가: official | company | research | news | other


class DataRecord(BaseModel):
    record_id: str
    source_id: str
    content: Any  # 원문 또는 구조화 자료
    locator: Optional[str] = None
    tags: list[str] = Field(default_factory=list)  # 자료 배분용 보조 정보


class Evidence(BaseModel):
    evidence_id: str
    record_id: str
    source_id: str
    excerpt: Optional[str] = None
    locator: Optional[str] = None
    # 임상 부가
    product_id: Optional[str] = None
    statement: Optional[str] = None  # 이 근거가 뒷받침하는 사실 한 문장
    event_date: Optional[str] = None
    geography: Optional[str] = None
    evidence_status: Optional[EvidenceStatus] = None


class Finding(BaseModel):
    finding_id: str
    category: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: Optional[str] = None
    related_criterion_ids: list[CriterionId] = Field(default_factory=list)


class MissingItem(BaseModel):
    missing_id: str
    item: str
    reason: str
    related_criterion_ids: list[CriterionId] = Field(default_factory=list)
    required_for_assessment: bool = False


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
    record_ids: list[str] = Field(default_factory=list)  # 이 범위에서 검토한 자료
    status: CoverageStatus
    note: Optional[str] = None
    # 임상 부가
    product_id: Optional[str] = None
    country: Optional[str] = None


class ReviewResponse(BaseModel):
    request_id: str
    question_id: str
    answer: str
    finding_ids: list[str] = Field(default_factory=list)
    resolved: bool = False


class WorkerError(BaseModel):
    code: str
    message: str


class WorkerResult(BaseModel):
    run_id: str
    company_id: str
    as_of: str
    schema_version: str = SCHEMA_VERSION
    criteria_version: str = CRITERIA_VERSION
    agent: WorkerName
    dataset_version: Optional[str] = None
    result_version: int
    analysis_status: AnalysisStatus
    data: dict  # 담당별 객체 (여기서는 ClinicalData)
    sources: list[dict] = Field(default_factory=list)  # 실제 참조한 Source (데이터셋 내용 그대로)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    review_responses: list[ReviewResponse] = Field(default_factory=list)
    errors: list[WorkerError] = Field(default_factory=list)


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
    record_ids: list[str] = Field(description="근거가 된 입력 자료의 record_id")
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
    record_ids: list[str] = Field(description="근거가 된 입력 자료의 record_id")
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
    record_ids: list[str] = Field(description="근거가 된 입력 자료의 record_id")
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


class ReviewAnswerDraft(BaseModel):
    question_id: str
    answer: str
    finding_ids: list[str] = Field(default_factory=list)
    resolved: bool = Field(description="제공 자료로 질문에 답했으면 true")


class ReviewAnswerOutput(BaseModel):
    answers: list[ReviewAnswerDraft] = Field(default_factory=list)


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


# ══════════════════════════════════════════════
# 6) 기존 06 심사(investment_review) 호환용 보완 요청 형식
#    평가 노드는 쓰지 않는다. 06이 새 계약으로 옮기면 삭제한다.
# ══════════════════════════════════════════════
ReviewReason = Literal[
    "missing_evidence", "conflict", "stale_information", "interpretation_error"
]


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
    target_agent: WorkerName
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
