from __future__ import annotations

try:
    from .. import schema as S
    from ..context import ClinicalRun
    from ..llm import _company_brief, ask
except ImportError:
    import schema as S
    from context import ClinicalRun
    from llm import _company_brief, ask


def claims_step(ctx: ClinicalRun) -> None:
    """5. 주장 대조: supported / partially_supported / contradicted / unknown"""
    claims = [c for p in ctx.company.products for c in p.claims]

    results: dict[str, S.ClaimCheckDraft] = {}
    if claims:
        evidence = [{k: e[k] for k in ("evidence_id", "product_id", "statement", "excerpt", "geography",
                                        "event_date", "evidence_status")}
                    for e in ctx.evidence.values()]
        try:
            out: S.ClaimExtraction = ask(S.ClaimExtraction, "claims", {
                "기업": _company_brief(ctx),
                "회사 주장": [c.model_dump() for c in claims],
                "근거(evidence)": evidence,
                "허가 기록": ctx.reg_records,
                "연구": ctx.studies,
                **ctx.review_payload(),
            })
            results = {r.claim_id: r for r in out.checks}
        except Exception as e:
            ctx.add_error("claim_check_failed", e)

    for c in claims:
        r = results.get(c.claim_id)
        ev = [e for e in (r.evidence_ids if r else []) if e in ctx.evidence]
        status = r.status if (r and ev) else "unknown"
        reason = r.comparison_reason if r else "대조 결과를 생성하지 못함"
        if r and not ev and r.status != "unknown":
            reason = f"유효한 근거 ID가 없어 unknown 처리: {r.comparison_reason}"
        ctx.claim_checks.append(S.ClaimCheck(claim_id=c.claim_id, product_id=c.product_id, original_claim=c.text,
                                             status=status, comparison_reason=reason, evidence_ids=ev).model_dump())
    for pid in ctx.pids:
        has = bool(ctx.product(pid).claims)
        info = ctx.classify.get(pid, {})
        if not has and info.get("regulatory_applicability") == "out_of_scope" and not info.get("has_efficacy_claim"):
            ctx.set_coverage("claim_check", pid, None, "not_applicable", note="규제 비대상·효능 주장 없음으로 대조 대상 없음")
            continue
        ctx.set_coverage("claim_check", pid, None, "reviewed" if has else "not_reviewed",
                         note=None if has else "company_profile에 회사 주장 원문이 없음")
