from __future__ import annotations

try:
    from .. import schema as S
    from ..context import ClinicalRun
    from ..llm import _company_brief, ask
except ImportError:
    import schema as S
    from context import ClinicalRun
    from llm import _company_brief, ask


def respond_step(ctx: ClinicalRun) -> None:
    """재검토 요청이 있으면 이번 평가 결과로 질문마다 답한다 (같은 자료, 추가 조회 없음)"""
    req = ctx.review_request
    questions = req.get("questions") or []
    if not questions:
        return
    answers: dict[str, S.ReviewAnswerDraft] = {}
    try:
        out: S.ReviewAnswerOutput = ask(S.ReviewAnswerOutput, "review", {
            "기업": _company_brief(ctx),
            **ctx.review_payload(),
            "이번 평가 요약": ctx.summary,
            "이번 평가의 Finding": ctx.findings,
            "부족 정보": ctx.missing_items,
        })
        answers = {a.question_id: a for a in out.answers}
    except Exception as e:
        ctx.add_error("review_response_failed", e)
    fids = {f["finding_id"] for f in ctx.findings}
    for q in questions:
        a = answers.get(q.get("question_id"))
        ctx.review_responses.append(S.ReviewResponse(
            request_id=req.get("request_id") or "", question_id=q.get("question_id") or "",
            answer=a.answer if a else "답변을 생성하지 못함",
            finding_ids=[f for f in (a.finding_ids if a else []) if f in fids],
            resolved=bool(a and a.resolved),
        ).model_dump())
