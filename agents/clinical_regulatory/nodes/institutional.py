import re

try:
    from .. import schema as S
    from ..context import ClinicalAgentState
    from ..llm import _company_brief, _product_brief, ask
    from ..tools import _coverage_status, _scope_of, web_search
    from ..utils import compare_to_as_of, norm_date, strip_fake_jan1
    from .regulatory import _mentions_target, _reassign_product
except ImportError:
    import schema as S
    from context import ClinicalAgentState
    from llm import _company_brief, _product_brief, ask
    from nodes.regulatory import _mentions_target, _reassign_product
    from tools import _coverage_status, _scope_of, web_search
    from utils import compare_to_as_of, norm_date, strip_fake_jan1


def _program_key(r: dict) -> tuple:
    return (r["product_id"], r["country"], r["program"])


def institutional_node(state: ClinicalAgentState) -> dict:
    """3. 제도권 진입: 지정·평가·등재·지불 경로 (허가 → 급여 추론 금지)"""
    ctx = state["ctx"]
    if "institutional" not in ctx.steps:
        return {}
    prev_des = [r for r in ctx.designations if r["product_id"] in ctx.target_pids]
    prev_rmb = [r for r in ctx.reimbursements if r["product_id"] in ctx.target_pids]
    ctx.designations = [r for r in ctx.designations if r["product_id"] not in ctx.target_pids]
    ctx.reimbursements = [r for r in ctx.reimbursements if r["product_id"] not in ctx.target_pids]

    groups: dict[tuple, list[dict]] = {}
    for pid in ctx.target_pids:
        if ctx.classify[pid]["regulatory_applicability"] == "out_of_scope":
            continue
        pn = (ctx.product_names(pid) or [ctx.company.display_name])[0]
        for country in ctx.product(pid).target_countries:
            if country == "KR":
                queries = [f"{ctx.company.display_name} 혁신의료기기 지정", f"{pn} 혁신의료기술 평가",
                           f"{pn} 비급여 등재 고시", f"{ctx.company.display_name} 건강보험 급여 등재"]
            elif country == "US":
                queries = [f"{pn} FDA Breakthrough Device designation", f"{pn} CMS reimbursement"]
            else:
                queries = [f"{pn} {country} reimbursement"]
            groups[(pid, country)] = [web_search(ctx, q) for q in queries]

    drafts: list[S.ProgramDraft] = []
    if groups and any(h["ok"] for hs in groups.values() for h in hs):
        try:
            out: S.ProgramExtraction = ask(S.ProgramExtraction, "institutional", {
                "기업": _company_brief(ctx),
                "제품·국가별 원자료": [{"product": _product_brief(ctx, pid), "country": c, "hits": hs}
                                  for (pid, c), hs in groups.items()],
                "확인된 허가 기록": [r for r in ctx.reg_records if r["product_id"] in ctx.target_pids],
            })
            drafts = out.records
        except Exception as e:
            ctx.validation_issues.append(f"제도 기록 추출 실패: {e}"[:200])

    private_accel_re = re.compile(r"엑셀러레이터|액셀러레이터|accelerator|인큐베이팅|챌린지|경진대회|cancerx|민간|데모데이", re.I)

    found: set[tuple] = set()
    for d in drafts:
        if d.status in ("not_found", "not_applicable"):
            continue
        # 민간 엑셀러레이터·펀드·경진대회 프로그램은 정부/공공 제도 지정(designation)에서 제외
        if d.kind == "designation" and private_accel_re.search((d.program or "") + " " + (d.statement or "")):
            continue

        source_titles = [ctx.sources.get(s, {}).get("title", "") for s in d.source_ids]
        d.product_id = _reassign_product(ctx, d.product_id, d.scope, d.statement, d.excerpt, *source_titles)
        if (d.product_id, d.country) not in groups:
            continue
        if not _mentions_target(ctx, d.product_id, d.statement, d.excerpt, d.source_ids):
            continue  # 제도 일반 설명(법령·가이드)만 있는 경우 제외
        eff = strip_fake_jan1(norm_date(d.effective_at), (d.statement or "") + " " + (d.excerpt or ""))
        if compare_to_as_of(eff, ctx.as_of) == "after":
            continue
        eid = ctx.add_evidence(d.product_id, d.statement, d.source_ids, d.evidence_status, d.excerpt, eff, d.country)
        if not eid:
            continue
        rec = {"product_id": d.product_id, "country": d.country, "program": d.program, "status": d.status,
               "scope": d.scope, "effective_at": eff, "evidence_ids": [eid]}
        if d.kind == "designation":
            rid = ctx.reuse_id(prev_des, "record_id", _program_key, _program_key(rec)) or ctx.new_id("des")
            ctx.designations.append(S.ProgramRecord(record_id=rid, **rec).model_dump())
        else:
            rid = ctx.reuse_id(prev_rmb, "record_id", _program_key, _program_key(rec)) or ctx.new_id("rmb")
            ctx.reimbursements.append(S.ProgramRecord(record_id=rid, **rec).model_dump())
        found.add((d.kind, d.product_id, d.country))

    for pid in ctx.target_pids:
        for country in ctx.product(pid).target_countries:
            for kind, area in (("designation", "designation"), ("reimbursement", "reimbursement")):
                if ctx.classify[pid]["regulatory_applicability"] == "out_of_scope":
                    ctx.set_coverage(area, pid, country, "reviewed", note="규제 비대상으로 해당 없음")
                    continue
                hits = groups.get((pid, country), [])
                ctx.set_coverage(area, pid, country, _coverage_status(hits, (kind, pid, country) in found),
                                 **_scope_of(hits))
    return {}
