"""04 traction_analysis(agents/traction_growth/schema.py의 AnalysisEnvelope)를 그대로 읽는다.

사용: data.criteria_inputs.C5, data.red_flags(RF1~RF4), data.open_questions, missing_items, sources.
RF는 경고(실사 질문)이며 자동 탈락이 아니므로 concerns로만 옮긴다.
"""
from __future__ import annotations

from ..contract import AgentResult
from ._common import criterion_from_input, failure_issue, missing_texts

AGENT = "traction"


def adapt_traction(env: dict) -> AgentResult:
    data = env.get("data") or {}
    return AgentResult(
        agent=AGENT, company_id=env.get("company_id"), analysis_status=env.get("analysis_status"),
        result_version=env.get("result_version"),
        criteria=[criterion_from_input("C5", (data.get("criteria_inputs") or {}).get("C5"), AGENT)],
        issues=failure_issue(AGENT, env.get("company_id"), env.get("analysis_status")),
        concerns=[f"{r['code']}({r.get('status')}): {r.get('reason', '')}" for r in data.get("red_flags") or []],
        due_diligence_questions=list(data.get("open_questions") or []),
        unknowns=missing_texts(env),
        source_ids=[s["source_id"] for s in env.get("sources") or []],
    )
