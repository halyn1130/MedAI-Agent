"""02 clinical_analysis(agents/clinical_regulatory/schema.py의 AnalysisEnvelope)를 그대로 읽는다.

State["clinical_analysis"]는 {company_id: Envelope}이므로 기업 하나의 Envelope를 넘긴다.
사용: data.criteria_inputs.C1, data.red_flags(CL01~CL03), coverage(regulatory), missing_items, sources.

G01 매핑 (CL02 = 목표국 공식 조치로 핵심 제품 현재 운영 차단)
- 분석 실패 또는 regulatory 조사 미완료 → not_checked
- CL02 confirmed → confirmed / CL02 candidate → unresolved / 그 외 → clear
"""
from __future__ import annotations

from ..contract import AgentResult, GateResult, GateStatus
from ._common import criterion_from_input, envelope_fields

AGENT = "clinical"
CL02_TO_G01 = {"confirmed": GateStatus.CONFIRMED, "candidate": GateStatus.UNRESOLVED}
REVIEWED = {"reviewed", "no_relevant_evidence_found"}


def _g01(env: dict) -> GateResult:
    if env.get("analysis_status") == "failed":
        return GateResult(code="G01", status=GateStatus.NOT_CHECKED, reason="임상 분석 실패")
    regulatory = [c for c in env.get("coverage") or [] if c.get("area") == "regulatory"]
    if not regulatory or any(c.get("status") not in REVIEWED for c in regulatory):
        return GateResult(code="G01", status=GateStatus.NOT_CHECKED, reason="공식 규제 기록 조사 미완료")
    cl02 = [f for f in env["data"].get("red_flags") or [] if f.get("code") == "CL02" and f.get("status") in CL02_TO_G01]
    if not cl02:
        return GateResult(code="G01", status=GateStatus.CLEAR, reason="CL02 미발견. 안전의 증명 아님")
    status = GateStatus.CONFIRMED if any(f["status"] == "confirmed" for f in cl02) else GateStatus.UNRESOLVED
    return GateResult(code="G01", status=status, reason="; ".join(f.get("description", "") for f in cl02),
                      evidence_ids=sorted({i for f in cl02 for i in f.get("evidence_ids") or []}))


def adapt_clinical(env: dict) -> AgentResult:
    data = env.get("data") or {}
    flags = [f for f in data.get("red_flags") or [] if f.get("status") != "resolved"]
    return AgentResult(
        **envelope_fields(env, AGENT),
        criteria=[criterion_from_input("C1", (data.get("criteria_inputs") or {}).get("C1"), AGENT)],
        gates=[_g01(env)],
        concerns=[f"{f['code']}({f['status']}, {f.get('materiality')}): {f.get('description', '')}" for f in flags],
    )
