import re

try:
    from .. import schema as S
    from ..context import ClinicalRun
    from ..llm import _company_brief, _product_brief, ask
    from ..utils import compare_to_as_of, norm_date, strip_fake_jan1
    from .regulatory import _mentions_target, _reassign_product
except ImportError:
    import schema as S
    from context import ClinicalRun
    from llm import _company_brief, _product_brief, ask
    from steps.regulatory import _mentions_target, _reassign_product
    from utils import compare_to_as_of, norm_date, strip_fake_jan1


def institutional_step(ctx: ClinicalRun) -> None:
    """3. 제도권 진입: 지정·평가·등재·지불 경로 (허가 → 급여 추론 금지)"""
    groups = {(pid, country): ctx.records_for("institutional", pid, country)
              for pid in ctx.pids if ctx.classify[pid]["regulatory_applicability"] != "out_of_scope"
              for country in ctx.product(pid).target_countries}

    drafts: list[S.ProgramDraft] = []
    failed = False
    if any(groups.values()):
        try:
            out: S.ProgramExtraction = ask(S.ProgramExtraction, "institutional", {
                "기업": _company_brief(ctx),
                "제품·국가별 원자료": [{"product": _product_brief(ctx, pid), "country": c,
                                   "records": [ctx.record_view(r) for r in rs]}
                                  for (pid, c), rs in groups.items() if rs],
                "확인된 허가 기록": ctx.reg_records,
                **ctx.review_payload(),
            })
            drafts = out.records
        except Exception as e:
            ctx.add_error("institutional_extraction_failed", e)
            failed = True

    private_accel_re = re.compile(r"엑셀러레이터|액셀러레이터|accelerator|인큐베이팅|챌린지|경진대회|cancerx|민간|데모데이", re.I)

    found: set[tuple] = set()
    for d in drafts:
        if d.status in ("not_found", "not_applicable"):
            continue
        # 민간 엑셀러레이터·펀드·경진대회 프로그램은 정부/공공 제도 지정(designation)에서 제외
        if d.kind == "designation" and private_accel_re.search((d.program or "") + " " + (d.statement or "")):
            continue

        source_titles = [ctx.record_title(r) for r in d.record_ids]
        d.product_id = _reassign_product(ctx, d.product_id, d.scope, d.statement, d.excerpt, *source_titles)
        if (d.product_id, d.country) not in groups:
            continue
        if not _mentions_target(ctx, d.product_id, d.excerpt, d.record_ids):
            continue  # 제도 일반 설명(법령·가이드)만 있는 경우 제외
        eff = strip_fake_jan1(norm_date(d.effective_at), (d.statement or "") + " " + (d.excerpt or ""))
        if compare_to_as_of(eff, ctx.as_of) == "after":
            continue
        eids = ctx.add_evidence(d.product_id, d.statement, d.record_ids, d.evidence_status, d.excerpt, eff, d.country)
        if not eids:
            continue
        rec = {"product_id": d.product_id, "country": d.country, "program": d.program, "status": d.status,
               "scope": d.scope, "effective_at": eff, "evidence_ids": eids}
        if d.kind == "designation":
            rid = ctx.new_id("des")
            ctx.designations.append(S.ProgramRecord(record_id=rid, **rec).model_dump())
        else:
            rid = ctx.new_id("rmb")
            ctx.reimbursements.append(S.ProgramRecord(record_id=rid, **rec).model_dump())
        found.add((d.kind, d.product_id, d.country))

    for pid in ctx.pids:
        for country in ctx.product(pid).target_countries:
            for area in ("designation", "reimbursement"):
                if ctx.classify[pid]["regulatory_applicability"] == "out_of_scope":
                    ctx.set_coverage(area, pid, country, "not_applicable", note="규제 비대상")
                    continue
                records = groups.get((pid, country), [])
                if not records:
                    ctx.set_coverage(area, pid, country, "not_reviewed", note="제공된 자료 없음")
                elif failed:
                    ctx.set_coverage(area, pid, country, "partial", records, note="자료 해석 실패")
                else:
                    ctx.set_coverage(area, pid, country, "reviewed", records,
                                     note=None if (area, pid, country) in found else "제공 자료에서 미발견")
