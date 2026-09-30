"""05 risk_analysis(risk-company-3)를 그대로 읽는다.

사용: areas[].observations/unknowns/due_diligence_questions, analysis_status, 상위 references.
- C6: 영역별 위험 신호(kind=risk_signal) 수로 채점한다. 규칙은 policy.C6_RULE.
      관찰이 있는 영역만 채점해 평균하고, 관찰이 있는 영역이 없으면 null.
- G02: 분석이 실행됐으면 clear(현재 중단 여부를 확정하는 필드는 없음), 실패·자료 없음이면 not_checked.
- 관찰은 source_linked_not_fact_verified이므로 위험 신호는 사실이 아니라 concerns(검토 대상)로 옮긴다.
- no_evidence는 실행 실패가 아니다. 자료 부족 ≠ 위험 없음.
"""
from __future__ import annotations

from typing import Optional

from ..contract import AgentResult, CriterionResult, GateResult, GateStatus, ScoreStatus
from ..policy import C6_AREA_SCORE_BY_SIGNALS, C6_AREA_SCORE_MANY_SIGNALS, C6_RULE
from ._common import failure_issue

AGENT = "risk"
RAN = {"complete", "partial"}
NOT_RUN_TEXT = {"no_evidence": "운영 리스크 분석에 쓸 공개 자료가 없음", "failed": "운영 리스크 분석 실패"}


def _signals(area: dict) -> list[dict]:
    return [o for o in area.get("observations") or [] if o.get("kind") == "risk_signal"]


def c6_from_areas(risk: dict) -> CriterionResult:
    base = dict(criterion_id="C6", source_agent=AGENT)
    if risk.get("analysis_status") not in RAN:
        return CriterionResult(**base, rationale=f"[{C6_RULE}] Risk 분석 상태 {risk.get('analysis_status')} → 채점 불가")
    scored, skipped, evidence = [], [], []
    for area in risk.get("areas") or []:
        if not area.get("observations"):
            skipped.append(area["category"])
            continue
        n = len(_signals(area))
        scored.append((area["category"], n, C6_AREA_SCORE_BY_SIGNALS.get(n, C6_AREA_SCORE_MANY_SIGNALS)))
        evidence += [c["passage_id"] for o in area["observations"] for c in o.get("citations") or []]
    if not scored:
        return CriterionResult(**base, rationale=f"[{C6_RULE}] 관찰이 있는 영역 없음 → 채점 불가")
    score = sum(s for _, _, s in scored) / len(scored)
    detail = ", ".join(f"{cat} 신호 {n}건={s:g}" for cat, n, s in scored)
    rationale = f"[{C6_RULE}] {detail} → 평균 {score:g}" + (f" (관찰 없음 제외: {', '.join(skipped)})" if skipped else "")
    return CriterionResult(**base, score=score, score_status=ScoreStatus.SCORED,
                           evidence_ids=sorted(set(evidence)), rationale=rationale)


def g02_from_status(risk: dict) -> GateResult:
    if risk.get("analysis_status") not in RAN:
        return GateResult(code="G02", status=GateStatus.NOT_CHECKED,
                          reason=NOT_RUN_TEXT.get(risk.get("analysis_status"), "운영 리스크 분석 결과 없음"))
    return GateResult(code="G02", status=GateStatus.CLEAR,
                      reason="Risk 분석에서 현재 중단을 확정하는 근거 없음. 안전의 증명 아님")


def adapt_risk(risk: dict, references: Optional[list] = None) -> AgentResult:
    areas = risk.get("areas") or []
    signals = [(a["category"], o) for a in areas for o in _signals(a)]
    return AgentResult(
        agent=AGENT, company_id=risk.get("company_id"), as_of=risk.get("as_of"),
        analysis_status=risk.get("analysis_status"),
        evidence_ids=[p["passage_id"] for p in risk.get("passages") or []],
        source_published={r["source_id"]: r.get("published_at") for r in references or []},
        criteria=[c6_from_areas(risk)],
        gates=[g02_from_status(risk)],
        issues=failure_issue(AGENT, risk.get("company_id"), risk.get("analysis_status")),
        concerns=[f"{cat}: {o['statement']}" + (f" — {o['conditional_impact']}" if o.get("conditional_impact") else "")
                  for cat, o in signals],
        due_diligence_questions=[q for a in areas for q in a.get("due_diligence_questions") or []],
        unknowns=[f"{a['category']}: {u}" for a in areas for u in a.get("unknowns") or []],
        source_ids=[r["source_id"] for r in references or []],
    )
