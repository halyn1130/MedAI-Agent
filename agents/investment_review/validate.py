"""취합·교차 검증: 어댑터 결과(AgentResult)를 대조해 ValidationIssue를 만든다.

검사 (issue_id = {company_id}:{agent}:{검사코드}[:{대상}])
- company_mismatch : 결과의 company_id가 심사 대상과 다름 → 판단불가, 보완 불가(입력 오류)
- as_of_mismatch   : 결과의 기준일이 심사 기준일과 다름 → 판단불가, 재분석 요청
- unknown_evidence : 채점된 점수가 결과 안에 없는 근거 ID를 참조 → 판단불가, 보완 요청
- no_evidence      : 채점된 점수에 근거 ID가 하나도 없음 → 판단불가, 보완 요청
- future_source    : 기준일 이후 발행 출처가 포함됨 → 경고, 보완 요청
- score_unknown    : 적용 항목 점수 미확인 → 경고(판정은 judge가 처리), 보완 요청

blocking=True 이슈가 보완 후에도 남으면 judge가 판단불가로 처리한다.
분석 실패(ANALYSIS_FAILED)는 어댑터가 이미 만든다.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .contract import AgentResult, IssueKind, ScoreStatus, ValidationIssue
from .policy import DEFAULT_POLICY, Policy


def _issue(company_id: str, agent: str, code: str, kind: IssueKind, *, blocking: bool,
           detail: str, criterion_id: Optional[str] = None, related_ids: Iterable[str] = (),
           reviewable: bool = True) -> ValidationIssue:
    suffix = f":{criterion_id}" if criterion_id else ""
    return ValidationIssue(issue_id=f"{company_id}:{agent}:{code}{suffix}", kind=kind, target_agent=agent,
                           criterion_id=criterion_id, blocking=blocking, reviewable=reviewable,
                           detail=detail, related_ids=sorted(set(related_ids)))


def validate_result(company_id: str, as_of: str, result: AgentResult,
                    policy: Policy = DEFAULT_POLICY) -> list[ValidationIssue]:
    agent, issues = result.agent, []
    if result.analysis_status == "not_run":
        return issues  # 결과 자체가 없음. 점수 unknown으로 judge가 처리

    if result.company_id != company_id:
        issues.append(_issue(company_id, agent, "company_mismatch", IssueKind.ANALYSIS_FAILED, blocking=True,
                             reviewable=False, detail=f"결과 company_id {result.company_id} ≠ 심사 대상 {company_id}"))
    if result.as_of != as_of:
        issues.append(_issue(company_id, agent, "as_of_mismatch", IssueKind.STALE_INFORMATION, blocking=True,
                             detail=f"결과 기준일 {result.as_of} ≠ 심사 기준일 {as_of}"))

    known = set(result.evidence_ids)
    for c in result.criteria:
        if c.criterion_id in policy.not_applicable:
            continue
        if c.score_status == ScoreStatus.SCORED:
            missing = [e for e in c.evidence_ids if e not in known]
            if missing:
                issues.append(_issue(company_id, agent, "unknown_evidence", IssueKind.INTERPRETATION_ERROR,
                                     blocking=True, criterion_id=c.criterion_id, related_ids=missing,
                                     detail=f"{c.criterion_id} 점수가 결과에 없는 근거 {len(missing)}건을 참조"))
            elif not c.evidence_ids:
                issues.append(_issue(company_id, agent, "no_evidence", IssueKind.MISSING_EVIDENCE, blocking=True,
                                     criterion_id=c.criterion_id, detail=f"{c.criterion_id} 점수에 근거 ID 없음"))
        else:
            issues.append(_issue(company_id, agent, "score_unknown", IssueKind.MISSING_EVIDENCE, blocking=False,
                                 criterion_id=c.criterion_id, related_ids=c.evidence_ids,
                                 detail=f"{c.criterion_id} 점수 미확인: {c.rationale}".rstrip(": ")))

    future = [sid for sid, published in result.source_published.items() if published and published[:10] > as_of]
    if future:
        issues.append(_issue(company_id, agent, "future_source", IssueKind.STALE_INFORMATION, blocking=False,
                             related_ids=future, detail=f"기준일 {as_of} 이후 발행 출처 {len(future)}건"))
    return issues


def validate(company_id: str, as_of: str, results: Iterable[AgentResult],
             policy: Policy = DEFAULT_POLICY) -> list[ValidationIssue]:
    """어댑터가 만든 이슈(분석 실패)와 교차 검증 이슈를 합쳐 반환한다."""
    out: list[ValidationIssue] = []
    for r in results:
        out += r.issues
        out += validate_result(company_id, as_of, r, policy)
    return out
