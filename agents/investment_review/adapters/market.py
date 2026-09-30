"""03 market_analysis(agents/market/schema.py의 AnalysisEnvelope)를 그대로 읽는다.

사용: data.criteria_inputs.C2·C3·C4, data.business_concerns, missing_items, sources.
영역 이름(size_growth·commercialization)은 설계서(market_growth·adoption)와 다르지만
criteria_inputs만 읽으므로 영향이 없다.
"""
from __future__ import annotations

from ..contract import AgentResult
from ._common import criterion_from_input, failure_issue, missing_texts

AGENT = "market"


def adapt_market(env: dict) -> AgentResult:
    data = env.get("data") or {}
    inputs = data.get("criteria_inputs") or {}
    return AgentResult(
        agent=AGENT, company_id=env.get("company_id"), analysis_status=env.get("analysis_status"),
        result_version=env.get("result_version"),
        criteria=[criterion_from_input(c, inputs.get(c), AGENT) for c in ("C2", "C3", "C4")],
        issues=failure_issue(AGENT, env.get("company_id"), env.get("analysis_status")),
        concerns=list(data.get("business_concerns") or []),
        unknowns=missing_texts(env),
        source_ids=[s["source_id"] for s in env.get("sources") or []],
    )
