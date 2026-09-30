"""실적·성장성 분석 에이전트(4번) 스키마 — 「04_실적·성장성_분석_변현준_v2」(COMMON CONTRACT 2.0) 기준.

schema_version = 2.0 / criteria_version = proposed-2.0

구성
0) 버전·설정값        : 점수·가중치·기간·예산 (문서상 '권장 기본안' → 설정값으로 관리)
1) 선택지(Enum)       : 상태값을 코드로 고정
2) 공통 근거 계약      : Source → Evidence → Finding, MissingItem, Coverage  (5개 문서 공통)
3) 관측값(records)    : 매출·계약·활동·고용·고객·검색 로그  (계산 입력)
4) 계산 결과          : 단계·성장·추이·기대치·레드플래그·C5  (코드가 채움)
5) 보완(Review)       : 심사 → ReviewRequest → ReviewResponse  (기업당 1라운드)
6) Envelope          : State["traction_analysis"] 에 들어가는 최종 객체
7) LLM 추출 스키마     : 기사·공시 원문 → 관측 후보 (LLM이 채우는 유일한 부분)

원칙
- LLM은 근거 분류·구조화 추출만 한다. 단계·성장률·추이·RF·C5는 코드가 계산한다.
- 알 수 없는 값은 null, 비적용은 not_applicable 로 구분한다.
- 빈 목록도 생략하지 않는다(default_factory=list).
- 실행 상태(analysis_status)와 사실 확인 상태(evidence_status / coverage)는 별개다.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────
# 0) 버전·설정값 (권장 기본안 — 팀 합의 시 여기만 바꾼다)
# ─────────────────────────────────────────────
SCHEMA_VERSION = "2.0"
CRITERIA_VERSION = "proposed-2.0"
AGENT_NAME = "traction"

STAGE_WINDOW_MONTHS = 36          # 상업화 단계: 최근 36개월 인정 근거 중 최고값
RF_WINDOW_MONTHS = 24             # RF1·RF3: as_of 기준 달력 24개월

GROWTH_G1_MIN = 0.50              # G1: r ≥ 0.50
GROWTH_G2_MIN = 0.20              # G2: 0.20 ≤ r < 0.50  /  G3: 0 ≤ r < 0.20  /  G4: r < 0
TREND_UP_MIN = 0.05               # 고용·고객 추이: ≥ +5% up, ≤ -5% down, 사이 flat
TREND_LATEST_MAX_AGE_MONTHS = 3   # 최신 관측은 as_of 이전 3개월 이내
TREND_BASE_GAP_MONTHS = (11, 13)  # 비교 기준값은 약 12개월 전(±1개월)
RF2_HEADCOUNT_DROP = -0.20        # RF2: 12개월 고용 감소율 ≤ -20%
RF3_MIN_MOU = 2                   # RF3: 24개월 내 서로 다른 MOU ≥ 2
INITIAL_MAX_CALLS = 40            # 최초 실행 기업당 외부 호출 예산 (4번 정의)
BUDGET_SPLIT = {"dart": 10, "news": 16, "nps": 14}   # 병렬 수집 노드별 몫 (합 = INITIAL_MAX_CALLS)
NEWS_MAX_AGE_MONTHS = 48          # 이보다 오래된 기사는 LLM 추출 대상에서 제외 (4번 정의)

C5_COMMERCIAL_SCORE = {"A": 5, "B": 4, "C": 2, "D": 1, "none": None}
C5_GROWTH_SCORE = {"G1": 5, "G2": 4, "G3": 3, "G4": 1, "G0": None}
C5_WEIGHTS = (0.7, 0.3)           # 둘 다 있으면 0.7·S + 0.3·T, T 없으면 S

# 투자 단계 → 최소 기대 상업화 단계. 목록 밖·값 없음 → expectation_status=unknown
STAGE_EXPECTATION = {
    "Pre-seed": "C", "Seed": "C",
    "Series A": "B", "Series B": "B",
    "Series C": "A", "Series D": "A", "Series E": "A", "Pre-IPO": "A",
}

MAX_REVIEW_ROUNDS = 1             # 기업당 보완 1라운드
REVIEW_MAX_CALLS = 5              # 보완 요청당 추가 검색 호출 수
RUNTIME_DEFAULTS = {              # 운영 기본값(권장 시작값). 재시도도 호출 예산에 포함
    "company_concurrency": 5,
    "external_call_concurrency": 8,
    "call_timeout_sec": 30,
    "transient_retry": 1,
}


# ─────────────────────────────────────────────
# 1) 선택지
# ─────────────────────────────────────────────
class AgentName(str, Enum):
    CLINICAL = "clinical"
    MARKET = "market"
    TRACTION = "traction"
    RISK = "risk"


class AnalysisStatus(str, Enum):      # 실행 상태 (근거 충분 여부 아님)
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ScoreStatus(str, Enum):
    SCORED = "scored"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class SourceType(str, Enum):
    OFFICIAL = "official"    # 공시·정부·공공 데이터
    COMPANY = "company"      # 기업 보도자료·홈페이지
    PARTNER = "partner"      # 거래 상대(병원·기관) 발표
    RESEARCH = "research"
    NEWS = "news"
    OTHER = "other"


class EvidenceStatus(str, Enum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


class CoverageStatus(str, Enum):
    REVIEWED = "reviewed"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    NO_RELEVANT_EVIDENCE_FOUND = "no_relevant_evidence_found"
    NOT_REVIEWED = "not_reviewed"
    SEARCH_FAILED = "search_failed"


class MissingCause(str, Enum):
    INPUT_MISSING = "input_missing"
    NOT_FOUND = "not_found"
    SEARCH_FAILED = "search_failed"
    NOT_REVIEWED = "not_reviewed"


class MissingRoute(str, Enum):
    PROFILE = "profile"
    CLINICAL = "clinical"
    MARKET = "market"
    TRACTION = "traction"
    RISK = "risk"
    DUE_DILIGENCE = "due_diligence"
    NONE = "none"


class Stage(str, Enum):
    A = "A"        # 인정된 양수 매출 관측
    B = "B"        # 인정된 유료 계약
    C = "C"        # 확인된 실증(PoC)
    D = "D"        # 확인된 MOU 또는 수상
    NONE = "none"  # 인정 근거 없음 ≠ 상업화 부재


class NoneReason(str, Enum):
    NO_RECOGNIZED_EVIDENCE = "no_recognized_evidence"
    SEARCH_INCOMPLETE = "search_incomplete"


class FinancialDisclosureStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    SEARCH_FAILED = "search_failed"
    NOT_REVIEWED = "not_reviewed"


class GrowthTier(str, Enum):
    G0 = "G0"  # 판단 불가
    G1 = "G1"  # r ≥ 0.50
    G2 = "G2"  # 0.20 ≤ r < 0.50
    G3 = "G3"  # 0 ≤ r < 0.20
    G4 = "G4"  # r < 0


class Trend(str, Enum):
    UP = "up"
    FLAT = "flat"
    DOWN = "down"
    UNKNOWN = "unknown"


class ExpectationStatus(str, Enum):
    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class RedFlagCode(str, Enum):
    RF1 = "RF1"  # 24개월 내 인정 신규 유료 계약 미발견 (needs_review만)
    RF2 = "RF2"  # 비교 가능한 매출 감소(G4) 또는 12개월 고용 ≤ -20%
    RF3 = "RF3"  # 24개월 내 서로 다른 MOU ≥ 2 & 인정 유료 계약·실증 미발견
    RF4 = "RF4"  # 확인된 투자 단계 대비 상업화 단계 기대치 미만


class RedFlagStatus(str, Enum):
    CONFIRMED = "confirmed"        # 수치 사실 확인 (RF2)
    NEEDS_REVIEW = "needs_review"  # 미발견·기대 미달 (RF1·RF3·RF4)


class ActivityType(str, Enum):
    POC = "poc"
    MOU = "mou"
    AWARD = "award"
    FUNDING = "funding"  # 상업화 판정 제외
    OTHER = "other"


class ContractStatus(str, Enum):   # 문서 예시값 signed 외 값은 4번 정의
    SIGNED = "signed"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"        # 철회·취소 → B 근거 제외, 이력 보존
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


class AccountingScope(str, Enum):  # 연결/별도 구분 (제품별 여부는 product_id로)
    CONSOLIDATED = "consolidated"
    SEPARATE = "separate"
    UNKNOWN = "unknown"


class SearchStatus(str, Enum):     # 정상 조회 미발견과 오류를 분리
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


class ReviewReason(str, Enum):
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICT = "conflict"
    STALE_INFORMATION = "stale_information"
    INTERPRETATION_ERROR = "interpretation_error"


class Resolution(str, Enum):
    RESOLVED = "resolved"
    PARTIALLY_RESOLVED = "partially_resolved"
    UNRESOLVED = "unresolved"
    SEARCH_FAILED = "search_failed"


class ChangeType(str, Enum):
    FACTS_UPDATED = "facts_updated"
    INTERPRETATION_UPDATED = "interpretation_updated"
    BOTH = "both"
    UNCHANGED = "unchanged"


class QuestionStatus(str, Enum):
    ANSWERED = "answered"
    PARTIALLY_ANSWERED = "partially_answered"
    UNANSWERED = "unanswered"


# ─────────────────────────────────────────────
# 2) 공통 근거 계약 : 출처 → 사실 → 판단
#    ID 예: c001:traction:src01 / c001:traction:ev01 / c001:traction:f01
#    보완 라운드에서도 ID 유지
# ─────────────────────────────────────────────
class Source(BaseModel):
    source_id: str
    title: str
    url: str
    publisher: str
    source_type: SourceType
    published_at: Optional[str] = Field(None, description="YYYY-MM-DD. 미상이면 null → 기준일 당시 존재 미확인 시 점수 근거 제외")
    checked_at: str = Field(description="실제 확인일 YYYY-MM-DD")
    original_source_id: Optional[str] = Field(None, description="재인용이면 원 출처 id (독립 근거 아님)")


class Evidence(BaseModel):
    evidence_id: str
    company_id: str
    product_id: Optional[str] = None
    statement: str = Field(description="확인된 사실 한 문장")
    source_ids: list[str] = Field(default_factory=list)
    locator: Optional[str] = Field(None, description="공시 항목·페이지·문단 위치")
    excerpt: Optional[str] = Field(None, description="원문 발췌(짧게)")
    event_date: Optional[str] = None
    geography: Optional[str] = None
    evidence_status: EvidenceStatus
    date_uncertainty: Optional[str] = Field(None, description="월 단위 날짜가 24/36개월 경계에 걸릴 때 사유")


class Finding(BaseModel):
    finding_id: str
    category: str = Field(description="stage | disclosure | growth | headcount | customer | expectation | red_flag | c5")
    claim: str = Field(description="판정 문장. 수치는 data 값과 일치해야 함")
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: Optional[str] = None
    related_criterion_ids: list[str] = Field(default_factory=lambda: ["C5"], description="C1~C6. RF/G 코드와 분리")


class MissingItem(BaseModel):
    item_id: str
    field: str = Field(description="예: revenue_by_year.2025, headcount_records")
    cause: MissingCause
    route: MissingRoute
    impact: str = Field(description="이 결측이 어떤 판정에 영향을 주는지")
    detail: str = ""


class Coverage(BaseModel):
    """조사 범위별 확인 상태. none·unknown을 '부재'로 오해하지 않게 함께 보고."""
    scope: str = Field(description="disclosure | revenue | contract | activity | headcount | customer")
    status: CoverageStatus
    search_log_indices: list[int] = Field(default_factory=list, description="data.search_log 인덱스")
    note: str = ""


# ─────────────────────────────────────────────
# 3) 관측값 : 원문 요약과 수치 입력을 분리
# ─────────────────────────────────────────────
class RevenueRecord(BaseModel):
    period_start: str
    period_end: str
    fiscal_year: int
    value: Optional[float] = None
    currency: str = "KRW"
    unit: str = Field("KRW", description="KRW | 천원 | 백만원 등 원문 단위")
    accounting_scope: AccountingScope = AccountingScope.UNKNOWN
    product_id: Optional[str] = Field(None, description="null=회사 전체, 값=제품별")
    evidence_ids: list[str] = Field(default_factory=list)


class ContractRecord(BaseModel):
    contract_id: str
    product_id: Optional[str] = None
    counterparty_id: Optional[str] = None
    counterparty_name: str
    event_date: Optional[str] = None
    contract_status: ContractStatus = ContractStatus.UNKNOWN
    is_paid: Optional[bool] = None
    independent_confirmation: bool = Field(description="보도자료 전재 기사면 false")
    evidence_ids: list[str] = Field(default_factory=list)


class ActivityRecord(BaseModel):
    activity_id: str
    type: ActivityType
    product_id: Optional[str] = None
    counterparty_id: Optional[str] = None
    date: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)


class HeadcountRecord(BaseModel):
    observation_date: str
    count: int
    population: str = Field(description="집계 모집단 정의. 예: 국민연금 가입자 수")
    entity_id: str = Field(description="사업장·법인 식별자. 비교 시 동일해야 함")
    evidence_ids: list[str] = Field(default_factory=list)


class CustomerRecord(BaseModel):
    observation_date: str
    customer_ids: list[str] = Field(default_factory=list)
    count: Optional[int] = Field(None, description="customer_ids 대신 수치만 있을 때")
    metric_definition: str = Field(description="도입 기관 / 유료 고객 등 정의")
    is_paid: Optional[bool] = None
    evidence_ids: list[str] = Field(default_factory=list)


class SearchLog(BaseModel):
    query: str
    searched_at: str
    status: SearchStatus
    entity_names: list[str] = Field(default_factory=list, description="조회에 쓴 정식명·이전 명칭·식별자")
    source_ids: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# ─────────────────────────────────────────────
# 4) 계산 결과 (코드)
# ─────────────────────────────────────────────
class RevenueYear(BaseModel):
    value: Optional[float] = None
    currency: str = "KRW"
    accounting_scope: AccountingScope = AccountingScope.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)


class GrowthDetail(BaseModel):
    observed_years: list[int] = Field(default_factory=list)
    periods: int = 0
    unavailable_reason: Optional[str] = Field(None, description="누락·시작 매출 ≤ 0·음수·범위 차이 등")


class TrendDetail(BaseModel):
    change_rate: Optional[float] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    definition: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)
    unavailable_reason: Optional[str] = None


class RedFlag(BaseModel):
    code: RedFlagCode
    finding_id: str = Field(description="RF 코드는 finding_id를 대신하지 않음")
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: RedFlagStatus


class C5Input(BaseModel):
    score: Optional[float] = None
    score_status: ScoreStatus = ScoreStatus.UNKNOWN
    commercial_score: Optional[int] = None
    growth_score: Optional[int] = None
    growth_metric: Optional[str] = Field(None, description="예: revenue_cagr")
    growth_status: ScoreStatus = ScoreStatus.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class CriteriaInputs(BaseModel):
    C5: C5Input = Field(default_factory=C5Input)


class TractionData(BaseModel):
    """Envelope.data — 관측값과 코드 계산 결과."""
    invest_stage: Optional[str] = Field(None, description="입력 CSV 값 보존")
    invest_stage_observed_at: Optional[str] = None

    financial_disclosure_status: FinancialDisclosureStatus = FinancialDisclosureStatus.NOT_REVIEWED
    commercial_stage: Stage = Stage.NONE
    none_reason: Optional[NoneReason] = None
    traction_recognized: Optional[bool] = Field(None, description="A/B=true, C/D=false, none=null")

    revenue_by_year: dict[int, RevenueYear] = Field(default_factory=dict)
    revenue_cagr: Optional[float] = Field(None, description="소수. 0.35 = 35%")
    growth_tier: GrowthTier = GrowthTier.G0
    growth_detail: GrowthDetail = Field(default_factory=GrowthDetail)

    headcount_trend: Trend = Trend.UNKNOWN
    headcount_detail: TrendDetail = Field(default_factory=TrendDetail)
    customer_trend: Trend = Trend.UNKNOWN
    customer_detail: TrendDetail = Field(default_factory=TrendDetail)

    expectation_status: ExpectationStatus = ExpectationStatus.UNKNOWN
    red_flags: list[RedFlag] = Field(default_factory=list)
    criteria_inputs: CriteriaInputs = Field(default_factory=CriteriaInputs)

    revenue_records: list[RevenueRecord] = Field(default_factory=list)
    contract_records: list[ContractRecord] = Field(default_factory=list)
    activity_records: list[ActivityRecord] = Field(default_factory=list)
    headcount_records: list[HeadcountRecord] = Field(default_factory=list)
    customer_records: list[CustomerRecord] = Field(default_factory=list)
    search_log: list[SearchLog] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────
# 5) 보완 (심사 → 해당 담당)  — 각 분석은 다른 에이전트를 직접 호출하지 않음
# ─────────────────────────────────────────────
class ReviewQuestion(BaseModel):
    question_id: str
    text: str


class SearchBudget(BaseModel):
    max_calls: int = REVIEW_MAX_CALLS


class ReviewContext(BaseModel):
    search_period: Optional[str] = None
    preferred_sources: list[str] = Field(default_factory=list)
    previous_search_history: list[SearchLog] = Field(default_factory=list)
    related_results: list[str] = Field(default_factory=list, description="참고할 다른 분석 키. 예: clinical_analysis")


class ReviewRequest(BaseModel):
    request_id: str
    company_id: str
    product_ids: list[str] = Field(default_factory=list)
    target_agent: AgentName
    finding_ids: list[str] = Field(default_factory=list)
    criterion_id: str = "C5"
    reason: ReviewReason
    blocker: str = Field(description="무엇 때문에 심사가 막혔는지")
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


# ─────────────────────────────────────────────
# 6) Envelope : State["traction_analysis"]
# ─────────────────────────────────────────────
class AnalysisEnvelope(BaseModel):
    run_id: str
    company_id: str
    as_of: str = Field(description="기준일 YYYY-MM-DD. 이후 공개 자료 제외")
    schema_version: str = SCHEMA_VERSION
    criteria_version: str = CRITERIA_VERSION
    agent: AgentName = AgentName.TRACTION
    result_version: int = 1
    analysis_status: AnalysisStatus = AnalysisStatus.COMPLETE
    data: TractionData = Field(default_factory=TractionData)

    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    review_responses: list[ReviewResponse] = Field(default_factory=list)


# ─────────────────────────────────────────────
# 7) LLM 추출 스키마 : llm.with_structured_output(ExtractionBatch)
#    LLM은 등급·성장률을 정하지 않는다. 원문에서 관측 후보만 뽑는다.
# ─────────────────────────────────────────────
class ExtractedItem(BaseModel):
    kind: Literal["revenue", "contract", "activity", "headcount", "customer", "none"]
    activity_type: Optional[ActivityType] = Field(None, description="kind=activity일 때만")
    statement: str = Field(description="원문 사실 한 문장. 추측 금지")
    excerpt: Optional[str] = Field(None, description="근거가 되는 원문 짧은 발췌")
    event_date: Optional[str] = Field(None, description="YYYY-MM 또는 YYYY-MM-DD. 모르면 null")
    product_name: Optional[str] = None
    counterparty_name: Optional[str] = None
    is_paid: Optional[bool] = Field(None, description="대가 명시 시 true/false, 불명확하면 null")
    contract_status: Optional[ContractStatus] = None
    value: Optional[float] = Field(None, description="매출·인원·고객 수 등 원문 수치")
    unit: Optional[str] = None
    fiscal_year: Optional[int] = None
    is_press_release_copy: bool = Field(False, description="회사 보도자료를 옮긴 기사면 true → independent_confirmation=false")
    source_url: str


class ExtractionBatch(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)


# 노드 반환 예:  return {"traction_analysis": envelope.model_dump(mode="json")}
TractionAnalysis = AnalysisEnvelope  # 기존 import 호환용 별칭

