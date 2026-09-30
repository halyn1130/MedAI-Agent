"""proposed-2.0 평가 정책. 정책값은 이 파일에서만 정의하고, 바꾸면 criteria_version을 올린다.

근거: README 「상세 평가 기준」, 통합 설계서 20~24쪽.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = "2.0"
CRITERIA_VERSION = "proposed-2.0"

CRITERIA = ("C1", "C2", "C3", "C4", "C5", "C6")
WEIGHTS = {"C1": 20, "C2": 10, "C3": 15, "C4": 15, "C5": 25, "C6": 15}
CRITERION_OWNER = {"C1": "clinical", "C2": "market", "C3": "market",
                   "C4": "market", "C5": "traction", "C6": "risk"}
GATES = ("G01", "G02")

SCORE_MIN, SCORE_MAX = 0.0, 5.0
ELIGIBLE_MIN_TOTAL = 60.0          # 총점 ≥ 60 (경계 포함)
CRITERION_MIN_SCORE = 2.0          # 적용 항목 모두 ≥ 2 (경계 포함)
DEFAULT_K = 5
TIEBREAK_CRITERIA = ("C5", "C1")   # 총점 동점 시 순서. 그다음 company_id 오름차순
TOTAL_DECIMALS = 6                 # 부동소수 오차로 60점 경계가 흔들리지 않도록 반올림

# C3·C4: 03 시장 체크(yes/no/unknown)를 06이 직접 센다 (06 자체 규칙).
# 영역 점수 = yes 개수. unknown은 점수에 넣지 않고 따로 표시한다. 확인된 체크(yes/no)가 없으면 null.
CHECK_RULE = "confirmed-checks-v1"
C3_DIMENSIONS = ("demand", "commercialization")   # C3 = 두 영역 평균
C4_DIMENSIONS = ("monetization",)

# ── 완화 규칙 (06 자체, 공개자료만으로 판단불가가 과도해 낮춘 기준) ──────────────
# ① C2 대체: C2가 null이면 03이 찾은 CAGR 수치(중앙값)로 설계서 구간에 따라 채점. 세부시장이 아닐 수 있음을 근거에 남긴다.
C2_CAGR_FALLBACK = True
C2_CAGR_BANDS = ((0.0, 0.0), (0.05, 1.0), (0.10, 2.0), (0.15, 3.0), (0.20, 4.0))  # (상한 미만, 점수), 그 이상 5
# ② C3 한쪽 허용: demand·commercialization 중 확인된 영역만으로 C3 계산.
C3_ALLOW_SINGLE_DIMENSION = True
# ③ 부분 판정: 채점된 가중치 비율이 이 값 이상이면 채점된 항목으로 판정 (Policy.min_coverage).
MIN_COVERAGE = 0.5
# ④ 0점 처리: 미확인(unknown) 적용 항목을 0점으로 채워 총점을 계산 (Policy.missing_as_zero).
#    0점으로 채운 항목에는 2점 최소 기준을 적용하지 않는다(적용하면 빈 항목이 있는 기업이 모두 부적격).
#    켜면 부분 판정(min_coverage)은 쓰이지 않는다.

# C6 운영 대비: 05 Risk 출력(risk-company-3)에 OP1~OP5가 없어 영역별 위험 신호로 채점한다 (06 자체 규칙).
# 관찰이 있는 영역만 채점하고 평균한다. 관찰이 있는 영역이 없으면 null (자료 없음 ≠ 위험 없음).
C6_RULE = "risk-signal-v1"
C6_AREA_SCORE_BY_SIGNALS = {0: 5.0, 1: 3.0}   # 위험 신호 수 → 영역 점수
C6_AREA_SCORE_MANY_SIGNALS = 1.0              # 2건 이상


@dataclass(frozen=True)
class Policy:
    """비교 모집단 전체에 같은 값을 적용한다. not_applicable은 기업별이 아니라 모집단 단위로 정한다."""
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    not_applicable: frozenset[str] = frozenset()
    min_total: float = ELIGIBLE_MIN_TOTAL
    min_criterion: float = CRITERION_MIN_SCORE
    k: int = DEFAULT_K
    tiebreak: tuple[str, ...] = TIEBREAK_CRITERIA
    min_coverage: float | None = MIN_COVERAGE   # None이면 설계서 기준(모든 적용 항목 필요)
    missing_as_zero: bool = False                # True면 미확인 항목 0점 처리 (④)
    criteria_version: str = CRITERIA_VERSION

    def __post_init__(self):
        if set(self.weights) != set(CRITERIA):
            raise ValueError(f"weights must define exactly {CRITERIA}")
        if any(w <= 0 for w in self.weights.values()):
            raise ValueError("weights must be positive")
        if not self.not_applicable <= set(CRITERIA):
            raise ValueError("unknown criterion in not_applicable")
        if not self.applicable:
            raise ValueError("at least one criterion must be applicable")  # README: 구성 오류
        if self.k < 0:
            raise ValueError("k must be >= 0")
        if self.min_coverage is not None and not 0 < self.min_coverage <= 1:
            raise ValueError("min_coverage must be in (0, 1]")

    @property
    def applicable(self) -> tuple[str, ...]:
        return tuple(c for c in CRITERIA if c not in self.not_applicable)


PARTIAL_POLICY = Policy()                        # 완화 기준: 부분 판정 50%
ZERO_FILL_POLICY = Policy(missing_as_zero=True)  # 완화 기준: 미확인 0점 처리
STRICT_POLICY = Policy(min_coverage=None)        # 설계서 기준
DEFAULT_POLICY = ZERO_FILL_POLICY
