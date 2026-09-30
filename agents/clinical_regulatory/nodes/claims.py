from __future__ import annotations

try:
    from .. import schema as S
    from ..context import ClinicalAgentState
    from ..llm import _company_brief, ask
except ImportError:
    import schema as S
    from context import ClinicalAgentState
    from llm import _company_brief, ask


def claims_node(state: ClinicalAgentState) -> dict:
    """5. 주장 대조: supported / partially_supported / contradicted / unknown"""
    ctx = state["ctx"]
    if "claims" not in ctx.steps:
        return {}
    claims = [c for pid in ctx.target_pids for c in ctx.product(pid).claims]
    claim_ids = {c.claim_id for c in claims}
    ctx.claim_checks = [c for c in ctx.claim_checks if c["claim_id"] not in claim_ids]

    results: dict[str, S.ClaimCheckDraft] = {}
    if claims:
        evidence = [{k: e[k] for k in ("evidence_id", "product_id", "statement", "geography", "event_date",
                                        "evidence_status")}
                    for e in ctx.evidence.values() if e["product_id"] in ctx.target_pids or e["product_id"] is None]
        try:
            out: S.ClaimExtraction = ask(S.ClaimExtraction, "claims", {
                "기업": _company_brief(ctx),
                "회사 주장": [c.model_dump() for c in claims],
                "근거(evidence)": evidence,
                "허가 기록": [r for r in ctx.reg_records if r["product_id"] in ctx.target_pids],
                "연구": [s for s in ctx.studies if s["product_id"] in ctx.target_pids],
            })
            results = {r.claim_id: r for r in out.checks}
        except Exception as e:
            ctx.validation_issues.append(f"주장 대조 실패: {e}"[:200])

    for c in claims:
        r = results.get(c.claim_id)
        ev = [e for e in (r.evidence_ids if r else []) if e in ctx.evidence]
        status = r.status if (r and ev) else "unknown"
        reason = r.comparison_reason if r else "대조 결과를 생성하지 못함"
        if r and not ev and r.status != "unknown":
            reason = f"유효한 근거 ID가 없어 unknown 처리: {r.comparison_reason}"
        ctx.claim_checks.append(S.ClaimCheck(claim_id=c.claim_id, product_id=c.product_id, original_claim=c.text,
                                             status=status, comparison_reason=reason, evidence_ids=ev).model_dump())
    for pid in ctx.target_pids:
        has = bool(ctx.product(pid).claims)
        info = ctx.classify.get(pid, {})
        if not has and info.get("regulatory_applicability") == "out_of_scope" and not info.get("has_efficacy_claim"):
            ctx.set_coverage("claim_check", pid, None, "reviewed", note="규제 비대상·효능 주장 없음으로 대조 대상 없음")
            continue
        ctx.set_coverage("claim_check", pid, None, "reviewed" if has else "insufficient_information",
                         note=None if has else "회사 주장 원문을 찾지 못함")
    return {}
