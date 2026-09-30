from __future__ import annotations

import json

try:
    from .. import schema as S
    from ..config import MAX_REVIEW_ROUNDS
    from ..context import ClinicalAgentState
    from ..llm import ask
    from .assemble import _data_dict, _referenced_sources
except ImportError:
    import schema as S
    from config import MAX_REVIEW_ROUNDS
    from context import ClinicalAgentState
    from llm import ask
    from nodes.assemble import _data_dict, _referenced_sources


def _facts_sig(data: dict) -> str:
    keep = {
        "reg": sorted((r["product_id"], r["country"], r["procedure_type"], r.get("record_number") or "", r["status"],
                       r.get("permitted_use") or "") for r in data.get("regulatory_records", [])),
        "prog": sorted((r["product_id"], r["country"], r["program"], r["status"])
                       for r in data.get("designation_records", []) + data.get("reimbursement_records", [])),
        "st": sorted((s["product_id"], s["design"], s["evidence_level"], s["result"]) for s in data.get("clinical_studies", [])),
    }
    return json.dumps(keep, ensure_ascii=False, default=str)


def _interp_sig(data: dict) -> str:
    keep = {
        "c1": [(p["product_id"], p.get("score"), p["score_status"])
               for p in (data.get("criteria_inputs", {}).get("C1", {}) or {}).get("product_scores", [])],
        "claims": sorted((c["claim_id"], c["status"]) for c in data.get("claim_checks", [])),
        "flags": sorted((f["code"], f["description"]) for f in data.get("red_flags", [])),
    }
    return json.dumps(keep, ensure_ascii=False, default=str)


def respond_node(state: ClinicalAgentState) -> dict:
    ctx = state["ctx"]
    if ctx.mode != "review":
        return {}
    new_version = int(ctx.previous.get("result_version", 0)) + 1
    new_data = _data_dict(ctx)
    facts_changed = _facts_sig(ctx.prev_data) != _facts_sig(new_data)
    interp_changed = _interp_sig(ctx.prev_data) != _interp_sig(new_data)
    change = ("both" if facts_changed and interp_changed else "facts_updated" if facts_changed
              else "interpretation_updated" if interp_changed else "unchanged")
    prev_claims = {f["finding_id"]: f["claim"] for f in ctx.prev_findings}
    updated_fids = [f["finding_id"] for f in ctx.findings if prev_claims.get(f["finding_id"]) != f["claim"]]
    all_failed = ctx.search_attempts > 0 and ctx.search_ok == 0

    for r in ctx.over_limit:
        ctx.review_responses.append(S.ReviewResponse(
            response_id=ctx.new_id("rr"), request_id=r.request_id, company_id=ctx.cid, result_version=new_version,
            resolution="unresolved", change_type="unchanged",
            question_results=[S.QuestionResult(question_id=q.question_id, status="unanswered",
                                                answer="보완 라운드 한도를 초과하여 처리하지 않음") for q in r.questions],
            limitations=[f"max_review_rounds={MAX_REVIEW_ROUNDS} 초과 (요청 round={r.review_round})"],
        ).model_dump())

    for r in ctx.requests:
        answers: dict[str, S.QuestionAnswerDraft] = {}
        out = None
        try:
            out = ask(S.ReviewAnswerOutput, "review", {
                "보완 요청": r.model_dump(),
                "갱신된 요약": ctx.summary,
                "근거(evidence)": [{k: e[k] for k in ("evidence_id", "product_id", "statement", "event_date")}
                                 for e in ctx.evidence.values()],
                "갱신된 data": new_data,
                "결측 항목": ctx.missing_items,
                "이번 실행 신규 출처": ctx.new_source_ids,
            })
            answers = {a.question_id: a for a in out.answers}
        except Exception as e:
            ctx.validation_issues.append(f"보완 응답 생성 실패: {e}"[:200])
        qres = []
        for q in r.questions:
            a = answers.get(q.question_id)
            ev = [e for e in (a.evidence_ids if a else []) if e in ctx.evidence]
            qres.append(S.QuestionResult(question_id=q.question_id, status=a.status if a else "unanswered",
                                         answer=a.answer if a else "답변을 생성하지 못함", evidence_ids=ev))
        statuses = [q.status for q in qres]
        if all_failed:
            resolution = "search_failed"
        elif statuses and all(s == "answered" for s in statuses):
            resolution = "resolved"
        elif any(s in ("answered", "partially_answered") for s in statuses):
            resolution = "partially_resolved"
        else:
            resolution = "unresolved"
        limitations = list(out.limitations) if out else []
        if ctx.budget.max_calls is not None and ctx.budget.used >= ctx.budget.max_calls:
            limitations.append(f"검색 예산 소진 ({ctx.budget.used}/{ctx.budget.max_calls}회)")
        ctx.review_responses.append(S.ReviewResponse(
            response_id=ctx.new_id("rr"), request_id=r.request_id, company_id=ctx.cid, result_version=new_version,
            resolution=resolution, change_type=change, updated_finding_ids=updated_fids,
            new_source_ids=[x for x in ctx.new_source_ids if x in {r["source_id"] for r in _referenced_sources(ctx)}],
            question_results=qres,
            remaining_unknowns=(list(out.remaining_unknowns) if out else [])
            + [m["field"] for m in ctx.missing_items if m["impact"] == "high"],
            limitations=limitations,
        ).model_dump())
    return {}
