"""어댑터 공통 헬퍼."""
from __future__ import annotations

from typing import Optional

from ..contract import CriterionResult, IssueKind, ScoreStatus, ValidationIssue

def criterion_from_input(criterion_id: str, raw: Optional[dict], agent: str) -> CriterionResult:
    """Envelope의 criteria_inputs.Cx → CriterionResult. 값이 없으면 unknown."""
    if not raw:
        return CriterionResult(criterion_id=criterion_id, source_agent=agent,
                               rationale=f"{agent} 결과에 criteria_inputs.{criterion_id} 없음")
    status = ScoreStatus(raw.get("score_status") or "unknown")
    score = raw.get("score") if status == ScoreStatus.SCORED else None
    return CriterionResult(criterion_id=criterion_id, score=score, score_status=status, source_agent=agent,
                           evidence_ids=list(raw.get("evidence_ids") or []), rationale=raw.get("rationale") or "")


def failure_issue(agent: str, company_id: Optional[str], status: Optional[str]) -> list[ValidationIssue]:
    if status != "failed":
        return []
    return [ValidationIssue(issue_id=f"{company_id}:{agent}:failed", kind=IssueKind.ANALYSIS_FAILED,
                            target_agent=agent, blocking=True, detail=f"{agent} 분석 실패")]


def missing_texts(envelope: dict) -> list[str]:
    out = []
    for m in envelope.get("missing_items") or []:
        impact = f"[{m['impact']}] " if m.get("impact") else ""
        out.append(f"{impact}{m.get('field')}: {m.get('cause')}" + (f" — {m['detail']}" if m.get("detail") else ""))
    return out
