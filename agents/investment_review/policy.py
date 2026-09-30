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


@dataclass(frozen=True)
class Policy:
    """비교 모집단 전체에 같은 값을 적용한다. not_applicable은 기업별이 아니라 모집단 단위로 정한다."""
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    not_applicable: frozenset[str] = frozenset()
    min_total: float = ELIGIBLE_MIN_TOTAL
    min_criterion: float = CRITERION_MIN_SCORE
    k: int = DEFAULT_K
    tiebreak: tuple[str, ...] = TIEBREAK_CRITERIA
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

    @property
    def applicable(self) -> tuple[str, ...]:
        return tuple(c for c in CRITERIA if c not in self.not_applicable)


DEFAULT_POLICY = Policy()
