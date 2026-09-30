"""03 market_analysis(agents/market/schema.py의 AnalysisEnvelope)를 그대로 읽는다.

사용: data.criteria_inputs.C2·C3·C4, data.business_concerns, missing_items, sources.
영역 이름(size_growth·commercialization)은 설계서(market_growth·adoption)와 다르지만
criteria_inputs만 읽으므로 영향이 없다.
"""
from __future__ import annotations

from ..contract import AgentResult
from ._common import criterion_from_input, envelope_fields

AGENT = "market"


def adapt_market(env: dict) -> AgentResult:
    data = env.get("data") or {}
    inputs = data.get("criteria_inputs") or {}
    return AgentResult(
        **envelope_fields(env, AGENT),
        criteria=[criterion_from_input(c, inputs.get(c), AGENT) for c in ("C2", "C3", "C4")],
        concerns=list(data.get("business_concerns") or []),
    )
