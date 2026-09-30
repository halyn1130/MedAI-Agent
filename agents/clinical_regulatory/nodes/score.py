from __future__ import annotations

try:
    from .. import schema as S
    from ..context import ClinicalAgentState
    from ..scoring import _build_missing, _build_summary, aggregate_c1, c1_for_product
    from ..utils import _dedupe
except ImportError:
    import schema as S
    from context import ClinicalAgentState
    from scoring import _build_missing, _build_summary, aggregate_c1, c1_for_product
    from utils import _dedupe


def score_node(state: ClinicalAgentState) -> dict:
    ctx = state["ctx"]
    core_pid = max(ctx.company.products, key=lambda p: p.scope_weight).product_id if ctx.company.products else None
    cov_by_pid: dict[str, list[dict]] = {}
    for c in ctx.coverage.values():
        cov_by_pid.setdefault(c.get("product_id"), []).append(c)

    # ── 제품 평가 · C1 ─────────────────────────
    for p in ctx.company.products:  # 판단불가였지만 유효한 허가 기록이 있으면 규제 대상
        if ctx.classify[p.product_id]["regulatory_applicability"] == "unknown" and any(
                r["product_id"] == p.product_id and r["status"] == "active" for r in ctx.reg_records):
            ctx.classify[p.product_id] = {**ctx.classify[p.product_id], "regulatory_applicability": "in_scope"}
    per: dict[str, dict] = {}
    ctx.product_assessments = []
    for p in ctx.company.products:
        pid, info = p.product_id, ctx.classify[p.product_id]
        r = c1_for_product(ctx, pid)
        per[pid] = r
        covs = cov_by_pid.get(pid, [])
        not_found = any(x["product_id"] == pid and x["status"] == "not_found" and x["country"] in p.target_countries
                        for x in ctx.reg_records)
        bad = [c for c in covs if c["status"] in ("insufficient_information", "not_reviewed", "search_failed")]
        if covs and all(c["status"] == "search_failed" for c in covs):
            a_status = "failed"
        elif bad or not_found:
            a_status = "partial"
        else:
            a_status = "complete"
        reg_ev = [e for x in ctx.reg_records if x["product_id"] == pid for e in x["evidence_ids"]]
        ctx.product_assessments.append(S.ProductAssessment(
            product_id=pid, regulatory_applicability=info["regulatory_applicability"],
            product_type=info["product_type"], assessment_status=a_status, evidence_level=r["level"],
            clinical_score=r["score"], score_status=r["status"], evidence_ids=_dedupe(r["evidence_ids"] + reg_ev),
        ).model_dump())
    ctx.c1 = aggregate_c1(ctx, per)

    # ── Finding (ID 유지) ───────────────────────
    findings: list[dict] = []

    def add_finding(key: str, category: str, claim: str, ev: list[str], uncertainty=None, criteria=None) -> str:
        fid = ctx.prev_keys.get(f"finding|{key}") or ctx.new_id("fd")
        ctx.id_keys[f"finding|{key}"] = fid
        findings.append(S.Finding(finding_id=fid, category=category, claim=claim, evidence_ids=_dedupe(ev),
                                  uncertainty=uncertainty, related_criterion_ids=criteria or []).model_dump())
        return fid

    names = {p.product_id: (p.name or p.product_id) for p in ctx.company.products}
    for r in ctx.reg_records:
        desc = f"{names[r['product_id']]} [{r['country']}] {r['procedure_type']} · {r['status']}"
        if r.get("record_number"):
            desc += f" ({r['record_number']})"
        if r.get("permitted_use"):
            desc += f" / 사용 목적: \"{r['permitted_use']}\""
        unc = "조회 범위 내 미발견. 무허가를 뜻하지 않음" if r["status"] == "not_found" else (
            "조회 실패로 상태 불명" if r["status"] == "unknown" else None)
        add_finding(f"regulatory|{r['record_id']}", "regulatory", desc, r["evidence_ids"], unc)
    for kind, recs in (("designation", ctx.designations), ("reimbursement", ctx.reimbursements)):
        for r in recs:
            add_finding(f"{kind}|{r['record_id']}", kind,
                        f"{names[r['product_id']]} [{r['country']}] {r['program']} · {r['status']}"
                        + (f" / 범위: {r['scope']}" if r.get("scope") else ""), r["evidence_ids"])
    for pid, r in per.items():
        n_st = len([s for s in ctx.studies if s["product_id"] == pid])
        add_finding(f"clinical_evidence|{pid}", "clinical_evidence",
                    f"{names[pid]}: 임상 근거 {r['level']}, C1 {r['score'] if r['score'] is not None else '없음'}"
                    f" ({r['status']}), 연구 {n_st}건", r["evidence_ids"],
                    None if r["status"] == "scored" else r["rationale"], ["C1"])
    claim_fid: dict[str, str] = {}
    for c in ctx.claim_checks:
        if c["status"] != "supported":
            claim_fid[c["claim_id"]] = add_finding(
                f"claim|{c['claim_id']}", "claim", f"주장 \"{c['original_claim']}\" → {c['status']}: "
                f"{c['comparison_reason']}", c["evidence_ids"],
                "근거 부족" if c["status"] == "unknown" else None, ["C1"])

    # ── 레드플래그 (자동 탈락 없음, 심사가 검토) ──
    flags: list[dict] = []

    def add_flag(key: str, code: str, materiality: str, desc: str, ev: list[str]) -> None:
        fid_find = add_finding(f"red_flag|{key}", f"red_flag:{code}", desc, ev,
                               criteria=["C1"] if code in ("CL01", "CL03") else [])
        flag_id = ctx.prev_keys.get(f"flag|{key}") or ctx.new_id("rf")
        ctx.id_keys[f"flag|{key}"] = flag_id
        flags.append(S.RedFlag(flag_id=flag_id, finding_id=fid_find, code=code, status="candidate",
                               materiality=materiality, description=desc, evidence_ids=_dedupe(ev)).model_dump())

    for c in ctx.claim_checks:
        if c["status"] == "contradicted":
            add_flag(f"CL01|{c['claim_id']}", "CL01", "high" if c["product_id"] == core_pid else "medium",
                     f"회사 주장과 확인된 사용 목적/근거 충돌: \"{c['original_claim']}\" — {c['comparison_reason']}",
                     c["evidence_ids"])
    for r in ctx.reg_records:
        p = ctx.product(r["product_id"])
        still_active = any(x["product_id"] == r["product_id"] and x["country"] == r["country"]
                           and x["status"] == "active" for x in ctx.reg_records)
        if (r["status"] in ("revoked", "expired") and not still_active and r["country"] in p.target_countries
                and ctx.classify[r["product_id"]]["regulatory_applicability"] == "in_scope"):
            add_flag(f"CL02|{r['record_id']}", "CL02", "high",
                     f"목표국 {r['country']}의 공식 기록 상태 {r['status']}: {names[r['product_id']]} 운영 차단 가능 (G01 후보 근거)",
                     r["evidence_ids"])
    for pid, r in per.items():
        if r["conflict"]:
            add_flag(f"CL03|{pid}", "CL03", "medium", f"{names[pid]}: 주요 근거 간 결과 충돌 — C1 판단불가", r["evidence_ids"])
    ctx.red_flags = flags
    ctx.findings = findings

    # ── 결측 항목 (ID 유지) ─────────────────────
    ctx.missing_items = _build_missing(ctx, per)

    # ── 사람이 읽는 요약 (구조화 필드에서 생성 → 모순 방지) ──
    ctx.summary = _build_summary(ctx, per, names)
    return {}
