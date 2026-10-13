from __future__ import annotations

try:
    from .. import schema as S
    from ..config import AGENT
    from ..context import ClinicalRun
except ImportError:
    import schema as S
    from config import AGENT
    from context import ClinicalRun


def _referenced_sources(ctx: ClinicalRun) -> list[dict]:
    """근거(evidence)가 실제로 참조하는 출처만 반환 (검색에서 스쳐 간 무관한 페이지 제외)"""
    used = {e["source_id"] for e in ctx.evidence.values()}
    return [s for sid, s in ctx.sources.items() if sid in used]


def assemble(ctx: ClinicalRun) -> dict:
    incomplete = any(c["status"] in ("partial", "not_reviewed") for c in ctx.coverage.values())
    status = "partial" if (incomplete or ctx.validation_issues or ctx.errors) else "complete"
    data = S.ClinicalData(
        product_assessments=ctx.product_assessments, regulatory_records=ctx.reg_records,
        designation_records=ctx.designations, reimbursement_records=ctx.reimbursements,
        clinical_studies=ctx.studies, claim_checks=ctx.claim_checks, red_flags=ctx.red_flags,
        clinical_summary=ctx.summary, criteria_inputs={"C1": ctx.c1} if ctx.c1 else {},
        validation_issues=ctx.validation_issues,
    ).model_dump()
    return S.WorkerResult(
        run_id=ctx.run_id, company_id=ctx.cid, as_of=ctx.as_of, agent=AGENT,
        dataset_version=ctx.dataset_version, result_version=int(ctx.previous.get("result_version", 0)) + 1,
        analysis_status=status, data=data,
        sources=_referenced_sources(ctx), evidence=list(ctx.evidence.values()), findings=ctx.findings,
        missing_items=ctx.missing_items, coverage=list(ctx.coverage.values()),
        review_responses=ctx.review_responses, errors=ctx.errors,
    ).model_dump()
