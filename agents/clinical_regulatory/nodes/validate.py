from __future__ import annotations

try:
    from ..context import ClinicalAgentState
    from ..utils import compare_to_as_of
except ImportError:
    from context import ClinicalAgentState
    from utils import compare_to_as_of


def validate_node(state: ClinicalAgentState) -> dict:
    """6. 자체 검증 (의미 재검색 없음 · 결측은 심사에 반환)"""
    ctx = state["ctx"]
    issues = ctx.validation_issues
    ev_ids, src_ids = set(ctx.evidence), set(ctx.sources)
    pids = {p.product_id for p in ctx.company.products}
    for e in ctx.evidence.values():
        if not set(e["source_ids"]) <= src_ids:
            issues.append(f"{e['evidence_id']}: 존재하지 않는 source_id 참조")
    groups = [("regulatory_records", ctx.reg_records), ("designation_records", ctx.designations),
              ("reimbursement_records", ctx.reimbursements), ("clinical_studies", ctx.studies),
              ("claim_checks", ctx.claim_checks), ("red_flags", ctx.red_flags)]
    for name, rows in groups:
        for r in rows:
            if not set(r.get("evidence_ids", [])) <= ev_ids:
                issues.append(f"{name}: 존재하지 않는 evidence_id 참조")
            if r.get("product_id") and r["product_id"] not in pids:
                issues.append(f"{name}: 알 수 없는 product_id {r['product_id']}")
    fids = {f["finding_id"] for f in ctx.findings}
    for f in ctx.red_flags:
        if f["finding_id"] not in fids:
            issues.append(f"{f['flag_id']}: finding_id 누락")
    for r in ctx.reg_records + ctx.designations + ctx.reimbursements:
        if compare_to_as_of(r.get("effective_at"), ctx.as_of) == "after":
            issues.append(f"{r['record_id']}: 기준일 이후 날짜")
    seen: dict[tuple, str] = {}
    for r in ctx.reg_records:  # 같은 기록번호인데 상태가 다르면 상충
        if r.get("record_number"):
            k = (r["product_id"], r["country"], r["record_number"])
            if k in seen and seen[k] != r["status"]:
                issues.append(f"{r['record_id']}: 동일 기록번호의 상태 상충")
            seen[k] = r["status"]
    scored = {p["product_id"] for p in ctx.c1.get("product_scores", [])}
    if scored != pids:
        issues.append("criteria_inputs.C1.product_scores가 모든 제품을 포함하지 않음")
    if not ctx.summary:
        issues.append("clinical_summary 비어 있음")
    return {}
