"""C1~C6 점수 확정과 가중 총점 계산. 미확인은 null로 보존하고 재가중하지 않는다.

weighted_points[c] = weight[c] × score[c] / 5
total_score = sum(weighted_points) / sum(적용 weights) × 100
"""
from __future__ import annotations

from typing import Iterable, Optional

from .contract import CriterionResult, ScoreStatus
from .policy import CRITERIA, DEFAULT_POLICY, SCORE_MAX, TOTAL_DECIMALS, Policy


def normalize_criteria(results: Iterable[CriterionResult],
                       policy: Policy = DEFAULT_POLICY) -> list[CriterionResult]:
    """C1~C6을 빠짐없이 한 번씩, 정책의 적용 범위에 맞춰 정리하고 가중치를 채운다.

    - 입력에 없는 항목은 unknown으로 둔다.
    - 모집단에서 비적용인 항목은 not_applicable로 둔다.
    - 모집단에서 적용인데 기업별로 not_applicable이 오면 unknown으로 바꾼다(불리한 항목 제외 방지).
    """
    by_id: dict[str, CriterionResult] = {}
    for r in results:
        if r.criterion_id in by_id:
            raise ValueError(f"duplicate criterion: {r.criterion_id}")
        by_id[r.criterion_id] = r

    out = []
    for cid in CRITERIA:
        r = by_id.get(cid) or CriterionResult(criterion_id=cid, rationale="분석 결과 없음")
        if cid in policy.not_applicable:
            r = r.model_copy(update={"score": None, "score_status": ScoreStatus.NOT_APPLICABLE,
                                     "weight": None, "weighted_points": None})
        else:
            if r.score_status == ScoreStatus.NOT_APPLICABLE:
                r = r.model_copy(update={
                    "score_status": ScoreStatus.UNKNOWN,
                    "rationale": (r.rationale + " / " if r.rationale else "")
                                 + "모집단에서 적용 항목이므로 기업별 비적용 불가 → unknown"})
            weight = float(policy.weights[cid])
            points = weight * r.score / SCORE_MAX if r.score_status == ScoreStatus.SCORED else None
            r = r.model_copy(update={"weight": weight, "weighted_points": points})
        out.append(r)
    return out


def zero_fill(results: list[CriterionResult]) -> list[CriterionResult]:
    """미확인 적용 항목을 0점으로 채운다 (policy.missing_as_zero). 원래 근거는 rationale에 남긴다."""
    out = []
    for r in results:
        if r.score_status == ScoreStatus.UNKNOWN:
            r = r.model_copy(update={"score": 0.0, "score_status": ScoreStatus.SCORED, "zero_filled": True,
                                     "weighted_points": 0.0,
                                     "rationale": "[0점 처리] 미확인" + (f" — {r.rationale}" if r.rationale else "")})
        out.append(r)
    return out


def unknown_criteria(results: list[CriterionResult]) -> list[str]:
    return [r.criterion_id for r in results if r.score_status == ScoreStatus.UNKNOWN]


def total_score(results: list[CriterionResult]) -> Optional[float]:
    """normalize_criteria 결과를 받는다. 적용 항목 중 하나라도 unknown이면 null."""
    applied = [r for r in results if r.score_status != ScoreStatus.NOT_APPLICABLE]
    if not applied or any(r.score_status == ScoreStatus.UNKNOWN for r in applied):
        return None
    total = sum(r.weighted_points for r in applied) / sum(r.weight for r in applied) * 100
    return round(total, TOTAL_DECIMALS)


def reference_score(results: list[CriterionResult]) -> tuple[Optional[float], list[str], Optional[float]]:
    """채점된 항목만으로 계산한 참고 점수, 쓴 항목, 쓴 가중치 비율. 순위·선정에 쓰지 않는다."""
    applied = [r for r in results if r.score_status != ScoreStatus.NOT_APPLICABLE]
    scored = [r for r in applied if r.score_status == ScoreStatus.SCORED]
    if not scored:
        return None, [], None
    used = sum(r.weight for r in scored)
    total = sum(r.weighted_points for r in scored) / used * 100
    return (round(total, TOTAL_DECIMALS), [r.criterion_id for r in scored],
            round(used / sum(r.weight for r in applied), TOTAL_DECIMALS))
