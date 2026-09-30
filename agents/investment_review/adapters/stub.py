"""Agent 결과가 없을 때(미실행·State 누락) 판단불가로 이어지는 빈 결과를 만든다."""
from __future__ import annotations

from ..contract import AgentResult, CriterionResult, GateResult
from ..policy import CRITERION_OWNER

GATE_OWNER = {"G01": "clinical", "G02": "risk"}


def not_run(agent: str, company_id: str) -> AgentResult:
    reason = f"{agent} 결과 없음"
    return AgentResult(
        agent=agent, company_id=company_id, analysis_status="not_run",
        criteria=[CriterionResult(criterion_id=c, source_agent=agent, rationale=reason)
                  for c, owner in CRITERION_OWNER.items() if owner == agent],
        gates=[GateResult(code=g, reason=reason) for g, owner in GATE_OWNER.items() if owner == agent],
        unknowns=[reason],
    )
