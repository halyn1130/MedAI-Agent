"""전체 기업 취합, 적격 기업 정렬(총점→C5→C1→company_id)과 최대 K개 선정.

적격이 아닌 기업도 결과에서 빼지 않고 사유와 함께 반환한다.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .contract import FinalStatus, InvestmentReview, Selection
from .policy import DEFAULT_POLICY, Policy

NOT_SELECTED_REASON = {
    FinalStatus.INELIGIBLE: "부적격",
    FinalStatus.UNDETERMINED: "판단불가",
}


def _rank_key(review: InvestmentReview, policy: Policy):
    # 모집단에서 비적용인 동점 기준은 건너뛴다(모든 기업이 같은 값 0).
    tiebreak = [-(review.score_of(c) or 0.0) if c not in policy.not_applicable else 0.0
                for c in policy.tiebreak]
    return (-review.total_score, *tiebreak, review.company_id)


def select(reviews: Iterable[InvestmentReview], policy: Policy = DEFAULT_POLICY,
           k: Optional[int] = None) -> list[InvestmentReview]:
    """적격 기업은 순위순, 나머지는 company_id순으로 이어 붙여 반환한다."""
    reviews = list(reviews)
    k = policy.k if k is None else k
    ids = [r.company_id for r in reviews]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate company_id in reviews")

    eligible = sorted((r for r in reviews if r.final_status == FinalStatus.ELIGIBLE),
                      key=lambda r: _rank_key(r, policy))
    others = sorted((r for r in reviews if r.final_status != FinalStatus.ELIGIBLE),
                    key=lambda r: r.company_id)

    out = []
    for rank, r in enumerate(eligible, start=1):
        selected = rank <= k
        reason = f"적격 {len(eligible)}개 중 {rank}위" + ("" if selected else f", 선정 한도 K={k} 초과")
        if r.score_basis == "partial":
            reason += f" · 부분 판정 ({'·'.join(r.reference_criteria)}, 가중치 {r.reference_weight:.0%})"
        elif r.score_basis == "zero_filled":
            zeros = [c.criterion_id for c in r.criterion_results if c.zero_filled]
            reason += f" · 미확인 0점 처리 ({'·'.join(zeros)})"
        out.append(r.model_copy(update={"selection": Selection(rank=rank, selected=selected, reason=reason)}))
    for r in others:
        codes = ", ".join(c.value for c in r.reason_codes)
        reason = NOT_SELECTED_REASON[r.final_status] + (f" ({codes})" if codes else "")
        if r.total_score is None and r.reference_score is not None:
            reason += (f" · 참고 점수 {r.reference_score:.1f} "
                       f"({'·'.join(r.reference_criteria)}, 가중치 {r.reference_weight:.0%} 기준)")
        out.append(r.model_copy(update={"selection": Selection(reason=reason)}))
    return out
