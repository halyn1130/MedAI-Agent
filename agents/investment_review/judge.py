"""판정 우선순위 적용: eligible / ineligible / undetermined와 reason_codes.

1. G01·G02 confirmed                          → ineligible (점수는 계산 가능하면 참고용 보존)
2. 게이트 미해결·미조사 / 차단 이슈 / 적용 점수 unknown → undetermined, total_score=null
   (총점이 null이면 채점된 항목만으로 reference_score를 함께 남긴다)
   단, 부분 판정(policy.min_coverage): 게이트·차단 이슈 문제가 없고 채점 가중치 비율이 min_coverage 이상이면
   reference_score를 총점으로 써서 3·4로 판정한다 (score_basis=partial).
   0점 처리(policy.missing_as_zero): 미확인 항목을 0점으로 채워 총점을 계산한다 (score_basis=zero_filled).
   0점으로 채운 항목에는 2점 최소 기준을 적용하지 않는다.
3. 총점 ≥ 60 AND 모든 (채점된) 적용 항목 ≥ 2      → eligible
4. 그 외                                       → ineligible
"""
from __future__ import annotations

from typing import Iterable

from .contract import (CriterionResult, FinalStatus, GateResult, GateStatus, InvestmentReview,
                       IssueKind, ReasonCode, ScoreStatus, ValidationIssue)
from .policy import DEFAULT_POLICY, GATES, Policy
from .scoring import normalize_criteria, reference_score, total_score, unknown_criteria, zero_fill

ISSUE_REASON = {
    IssueKind.MISSING_EVIDENCE: ReasonCode.MISSING_EVIDENCE,
    IssueKind.STALE_INFORMATION: ReasonCode.MISSING_EVIDENCE,
    IssueKind.CONFLICT: ReasonCode.UNRESOLVED_CONFLICT,
    IssueKind.INTERPRETATION_ERROR: ReasonCode.UNRESOLVED_CONFLICT,
    IssueKind.ANALYSIS_FAILED: ReasonCode.ANALYSIS_FAILED,
}


def normalize_gates(gates: Iterable[GateResult]) -> list[GateResult]:
    """G01·G02를 한 번씩. 입력에 없는 게이트는 not_checked."""
    by_code: dict[str, GateResult] = {}
    for g in gates:
        if g.code in by_code:
            raise ValueError(f"duplicate gate: {g.code}")
        by_code[g.code] = g
    return [by_code.get(code) or GateResult(code=code, reason="조사 결과 없음") for code in GATES]


def _dedupe(codes: list[ReasonCode]) -> list[ReasonCode]:
    return list(dict.fromkeys(codes))


def judge(company_id: str, as_of: str,
          criteria: Iterable[CriterionResult] = (),
          gates: Iterable[GateResult] = (),
          issues: Iterable[ValidationIssue] = (),
          policy: Policy = DEFAULT_POLICY) -> InvestmentReview:
    results = normalize_criteria(criteria, policy)
    gate_results = normalize_gates(gates)
    issues = list(issues)
    unknowns = [f"{c}: 점수 미확인" for c in unknown_criteria(results)]
    filled = bool(unknowns) and policy.missing_as_zero
    if filled:
        results = zero_fill(results)
    total = total_score(results)
    unknowns += [f"{g.code}: {g.status.value}" for g in gate_results
                 if g.status in (GateStatus.UNRESOLVED, GateStatus.NOT_CHECKED)]
    unknowns += [f"{i.issue_id}: {i.detail}" for i in issues if i.blocking]
    base = dict(company_id=company_id, as_of=as_of, criteria_version=policy.criteria_version,
                criterion_results=results, gate_results=gate_results, remaining_unknowns=unknowns)
    if total is None:
        ref, ref_criteria, ref_weight = reference_score(results)
        base.update(reference_score=ref, reference_criteria=ref_criteria, reference_weight=ref_weight)

    # 1. 확인된 중대 차단
    blocked = [ReasonCode(g.code) for g in gate_results if g.status == GateStatus.CONFIRMED]
    if blocked:
        return InvestmentReview(**base, total_score=total, final_status=FinalStatus.INELIGIBLE,
                                reason_codes=blocked)

    # 2. 판단불가
    reasons = []
    if any(g.status in (GateStatus.UNRESOLVED, GateStatus.NOT_CHECKED) for g in gate_results):
        reasons.append(ReasonCode.MISSING_EVIDENCE)
    reasons += [ISSUE_REASON[i.kind] for i in issues if i.blocking]
    basis = "zero_filled" if filled else "full"
    if total is None:
        ref_weight = base.get("reference_weight")
        if (not reasons and policy.min_coverage is not None and ref_weight is not None
                and ref_weight >= policy.min_coverage):
            total, basis = base["reference_score"], "partial"
        else:
            reasons.append(ReasonCode.MISSING_EVIDENCE)
    if reasons:
        return InvestmentReview(**base, total_score=None, final_status=FinalStatus.UNDETERMINED,
                                reason_codes=_dedupe(reasons))

    # 3·4. 점수 기준
    if total < policy.min_total:
        reasons.append(ReasonCode.SCORE_BELOW_60)
    if any(r.score < policy.min_criterion for r in results
           if r.score_status == ScoreStatus.SCORED and not r.zero_filled):
        reasons.append(ReasonCode.CRITERION_BELOW_2)
    status = FinalStatus.INELIGIBLE if reasons else FinalStatus.ELIGIBLE
    return InvestmentReview(**base, total_score=total, score_basis=basis, final_status=status, reason_codes=reasons)
