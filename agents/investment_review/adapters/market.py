"""03 market_analysis(agents/market/schema.py의 AnalysisEnvelope)를 그대로 읽는다.

- C2: data.criteria_inputs.C2. null이면(C2_CAGR_FALLBACK) 03이 찾은 CAGR 수치의 중앙값을 설계서 구간으로 채점.
      세부시장이 아닌 상위 시장 수치일 수 있으므로 근거에 표시한다.
- C3·C4: data.dimensions의 체크를 06이 직접 센다 (policy.CHECK_RULE).
  영역 점수 = yes 개수, unknown은 점수에 넣지 않고 unconfirmed_checks·unknowns로 표시.
  확인된 체크(yes/no)가 하나도 없는 영역은 null. C3 = demand·commercialization 평균.
  C3_ALLOW_SINGLE_DIMENSION이면 확인된 영역만으로 계산하고, 둘 다 null일 때만 null.
  dimensions가 없으면 criteria_inputs를 그대로 쓴다.
영역 이름(size_growth·commercialization)은 설계서(market_growth·adoption)와 다르다.
"""
from __future__ import annotations

from statistics import median
from typing import Optional

from ..contract import AgentResult, CriterionResult, ScoreStatus
from ..policy import (C2_CAGR_BANDS, C2_CAGR_FALLBACK, C3_ALLOW_SINGLE_DIMENSION, C3_DIMENSIONS,
                      C4_DIMENSIONS, CHECK_RULE)
from ._common import criterion_from_input, envelope_fields

AGENT = "market"


def _dimension(dim: dict) -> tuple[Optional[float], list[dict]]:
    checks = dim.get("checks") or []
    known = [c for c in checks if c.get("status") in ("yes", "no")]
    unknown = [c for c in checks if c.get("status") not in ("yes", "no")]
    if not known:
        return None, unknown
    return float(sum(c["status"] == "yes" for c in known)), unknown


def criterion_from_checks(criterion_id: str, dims: dict, names: tuple[str, ...]) -> CriterionResult:
    scores, unknown, evidence, parts = [], [], [], []
    for name in names:
        dim = dims.get(name) or {}
        score, unk = _dimension(dim)
        scores.append(score)
        unknown += unk
        evidence += [e for c in dim.get("checks") or [] if c.get("status") == "yes" for e in c.get("evidence_ids") or []]
        n = len(dim.get("checks") or [])
        parts.append(f"{name} yes {score:g}/{n}" if score is not None else f"{name} 확인된 체크 없음")
    base = dict(criterion_id=criterion_id, source_agent=AGENT, unconfirmed_checks=[c["check_id"] for c in unknown])
    note = f"[{CHECK_RULE}] " + ", ".join(parts) + (f" · 미확인 {', '.join(c['check_id'] for c in unknown)}" if unknown else "")
    known = [s for s in scores if s is not None]
    if not known or (len(known) < len(scores) and not C3_ALLOW_SINGLE_DIMENSION):
        return CriterionResult(**base, rationale=note)
    if len(known) < len(scores):
        note += " · 확인된 영역만으로 계산(완화)"
    return CriterionResult(**base, score=sum(known) / len(known), score_status=ScoreStatus.SCORED,
                           evidence_ids=sorted(set(evidence)), rationale=note)


def cagr_score(rate: float) -> float:
    for upper, score in C2_CAGR_BANDS:
        if rate < upper:
            return score
    return 5.0


def c2_from_metrics(data: dict, original: CriterionResult) -> CriterionResult:
    """C2가 null일 때 03이 찾은 CAGR 수치로 채점 (완화)."""
    metrics = [m for m in (data.get("market_metrics") or []) + ((data.get("dimensions") or {})
               .get("size_growth") or {}).get("market_metrics", []) if m.get("metric_type") == "cagr"]
    unique = {(m.get("metric_id"), m["value"]): m for m in metrics}.values()
    if not unique:
        return original
    rate = median(m["value"] for m in unique)
    segments = sorted({str(m.get("segment")) for m in unique})
    return CriterionResult(
        criterion_id="C2", score=cagr_score(rate), score_status=ScoreStatus.SCORED, source_agent=AGENT,
        evidence_ids=sorted({e for m in unique for e in m.get("evidence_ids") or []}),
        rationale=f"[완화] 03 C2 null → CAGR 수치 {len(unique)}개 중앙값 {rate:.1%}로 채점. "
                  f"세부시장이 아닐 수 있음 (세그먼트: {', '.join(segments[:3])})")


def adapt_market(env: dict) -> AgentResult:
    data = env.get("data") or {}
    inputs = data.get("criteria_inputs") or {}
    dims = data.get("dimensions") or {}
    if dims:
        c3 = criterion_from_checks("C3", dims, C3_DIMENSIONS)
        c4 = criterion_from_checks("C4", dims, C4_DIMENSIONS)
    else:
        c3, c4 = (criterion_from_input(c, inputs.get(c), AGENT) for c in ("C3", "C4"))
    unconfirmed = [f"{c.criterion_id} 체크 미확인: {chk.get('check_id')} — {chk.get('rationale', '')}"
                   for c, names in ((c3, C3_DIMENSIONS), (c4, C4_DIMENSIONS))
                   for name in names for chk in (dims.get(name) or {}).get("checks") or []
                   if chk.get("status") not in ("yes", "no")]
    c2 = criterion_from_input("C2", inputs.get("C2"), AGENT)
    if c2.score_status == ScoreStatus.UNKNOWN and C2_CAGR_FALLBACK:
        c2 = c2_from_metrics(data, c2)
    fields = envelope_fields(env, AGENT)
    fields["unknowns"] = fields["unknowns"] + unconfirmed
    return AgentResult(
        **fields,
        criteria=[c2, c3, c4],
        concerns=list(data.get("business_concerns") or []),
    )
