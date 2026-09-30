from __future__ import annotations

try:
    from .. import schema as S
    from ..config import AGENT, AREA_BY_STEP
    from ..context import ClinicalAgentState, ClinicalRun
except ImportError:
    import schema as S
    from config import AGENT, AREA_BY_STEP
    from context import ClinicalAgentState, ClinicalRun


def _data_dict(ctx: ClinicalRun) -> dict:
    return S.ClinicalData(
        product_assessments=ctx.product_assessments, regulatory_records=ctx.reg_records,
        designation_records=ctx.designations, reimbursement_records=ctx.reimbursements,
        clinical_studies=ctx.studies, claim_checks=ctx.claim_checks, red_flags=ctx.red_flags,
        clinical_summary=ctx.summary, criteria_inputs={"C1": ctx.c1} if ctx.c1 else {},
        validation_issues=ctx.validation_issues, id_keys=ctx.id_keys, collected_profile=ctx.collected,
    ).model_dump()


def _referenced_sources(ctx: ClinicalRun) -> list[dict]:
    """근거(evidence)가 실제로 참조하는 출처만 반환 (검색에서 스쳐 간 무관한 페이지 제외)"""
    used = {sid for e in ctx.evidence.values() for sid in e["source_ids"]}
    return [s for sid, s in ctx.sources.items() if sid in used]


def assemble_node(state: ClinicalAgentState) -> dict:
    ctx = state["ctx"]
    this_run = [c for c in ctx.coverage.values()
                if c.get("product_id") in ctx.target_pids and any(c["area"] in AREA_BY_STEP[s] for s in ctx.steps)]
    incomplete = any(c["status"] in ("search_failed", "not_reviewed") for c in this_run)
    status = "partial" if (incomplete or ctx.validation_issues) else "complete"
    findings = ctx.findings
    env = S.AnalysisEnvelope(
        run_id=ctx.run_id, company_id=ctx.cid, as_of=ctx.as_of, agent=AGENT,
        result_version=int(ctx.previous.get("result_version", 0)) + 1, analysis_status=status,
        data=_data_dict(ctx),
        sources=_referenced_sources(ctx), evidence=list(ctx.evidence.values()), findings=findings,
        missing_items=ctx.missing_items, coverage=list(ctx.coverage.values()),
        review_responses=list(ctx.previous.get("review_responses", [])) + ctx.review_responses,
    )
    return {"envelope": env.model_dump()}
