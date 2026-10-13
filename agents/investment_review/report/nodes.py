"""보고서 LangGraph 노드: 데이터 취합 → 보고서 작성 → 보고서 평가 (미달 시 작성으로 Loop).

    (select) → aggregate → write_report → evaluate_report ─(retry: 고칠 수 있는 미달 & 시도 < 상한)→ write_report
                                                         └─(passed · unfixable · max_attempts)──────→ END

- aggregate      : final_reviews + 02~05 분석 결과 + 기업 프로필 → report_context (build_context, PubMed 서지, 데이터 한계)
- write_report   : LLM 서술(writer) → 숫자·코드 검증(check_narrative) → Markdown(render). 반려된 항목만 다시 쓴다.
- evaluate_report: 규칙 검사 + LLM Judge (evaluator.evaluate) → report_status 결정

State 설계
- 페이로드: report_context(취합 1회), narrative·report_markdown(시도마다 덮어씀, 누적 없음), eval_result(마지막 평가)
- 제어   : report_attempt·max_report_attempts(종료 보장), eval_feedback(작성 노드에 넘기는 반려 사유),
           report_status(passed / retry / unfixable / max_attempts)
- eval_history: 시도별 평가 요약. reducer로 누적하지만 시도 상한만큼만 쌓인다.
- 라우팅 결정·사유는 State가 아니라 logger(JSON)로 남기고 trace_id로 LangSmith·외부 로그와 잇는다.
"""
from __future__ import annotations

import json
import logging
import operator
from pathlib import Path
from typing import Annotated, Any, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .checker import check_narrative
from .citations import fetch_pubmed, pubmed_metadata
from .context_builder import build_context
from .evaluator import data_limitations, evaluate, get_judge, note_key
from .renderer import render
from .writer import get_llm, write

MAX_REPORT_ATTEMPTS = 3
log = logging.getLogger("investment_review.report")


class ReportFields(TypedDict, total=False):
    """전체 그래프(RunState)와 보고서 단독 그래프(ReportState)가 함께 쓰는 보고서 필드."""
    # ── 제어 ──
    trace_id: str
    meta: dict                                   # as_of·criteria_version·policy·k
    report_attempt: int
    max_report_attempts: int
    eval_feedback: list[str]
    report_status: str
    # ── 페이로드 ──
    report_context: dict
    narrative: dict
    writer_notes: list[str]
    report_markdown: str
    eval_result: dict
    eval_history: Annotated[list, operator.add]


class ReportState(ReportFields, total=False):
    final_reviews: list
    analyses: dict                               # {company_id: {clinical_analysis, ...}}
    companies: dict                              # {company_id: 프로필}


def _log(state: dict, node: str, decision: str, reason: str) -> None:
    log.info(json.dumps({"trace_id": state.get("trace_id"), "node": node, "attempt": state.get("report_attempt"),
                         "decision": decision, "reason": reason}, ensure_ascii=False))


def _by_id(items) -> dict:
    """전체 그래프는 list(기업별 결과), 보고서 CLI는 {company_id: ...}를 넘긴다."""
    return items if isinstance(items, dict) else {x["company_id"]: x for x in items or []}


def aggregate_node(pubmed_cache: Optional[Path] = None, fetch: Optional[Callable] = fetch_pubmed):
    def aggregate(state: dict) -> dict:
        ctx = build_context(state.get("final_reviews") or [], _by_id(state.get("analyses")),
                            _by_id(state.get("companies")), state["meta"])
        ctx["pubmed"] = pubmed_metadata(ctx["references"], pubmed_cache, fetch or (lambda _ids: {}))
        ctx["limitations"] = data_limitations(ctx)
        _log(state, "aggregate", "context_ready",
             f"선정 {len(ctx['selected'])}개 · 출처 {len(ctx['references'])}건 · 데이터 한계 {len(ctx['limitations'])}건")
        return {"report_context": ctx, "report_attempt": 0, "eval_feedback": [], "narrative": {}}
    return aggregate


def write_node(llm: Any = None):
    """llm=None이면 템플릿 문장만. 반려 사유(eval_feedback)가 있으면 그 항목만 바꾸고 통과한 항목은 유지한다."""
    def write_report(state: dict) -> dict:
        ctx = state["report_context"]
        prev = dict(state.get("narrative") or {})
        feedback = state.get("eval_feedback") or []
        narrative, notes = prev, []
        if llm is not None:
            raw = write(ctx, llm, feedback=feedback or None)
            if raw is None:
                notes.append("LLM 서술 없음(모델 미설정 또는 호출 실패) → 템플릿 문장")
            else:
                clean, notes = check_narrative(raw, ctx)
                if feedback:
                    failed = {note_key(f) for f in feedback}
                    narrative = {k: v for k, v in prev.items() if k not in failed}
                    narrative.update({k: v for k, v in clean.items() if k in failed or k not in narrative})
                    # 유지한 항목의 새 출력이 걸린 것은 반려 사유가 아니다 (이전 통과본을 쓴다)
                    notes = [n for n in notes if note_key(n) not in prev or note_key(n) in failed]
                else:
                    narrative = clean
        return {"narrative": narrative, "writer_notes": notes, "report_markdown": render(ctx, narrative),
                "report_attempt": state.get("report_attempt", 0) + 1}
    return write_report


def evaluate_node(judge: Any = None):
    def evaluate_report(state: dict) -> dict:
        verdict = evaluate(state["report_markdown"], state["report_context"], state.get("narrative") or {},
                           state.get("writer_notes") or [], judge)
        attempt = state["report_attempt"]
        cap = state.get("max_report_attempts") or MAX_REPORT_ATTEMPTS
        if verdict["passed"]:
            status, reason = "passed", "모든 기준 통과"
        elif not verdict["feedback"]:
            status, reason = "unfixable", "작성 노드가 고칠 수 없는 미달(데이터·형식) → 종료, 한계는 보고서에 공개"
        elif attempt >= cap:
            status, reason = "max_attempts", f"시도 상한 {cap}회 도달 → 마지막 보고서로 종료"
        else:
            status, reason = "retry", f"반려 {len(verdict['feedback'])}건 → 보고서 작성으로 재요청"
        failed = [k for k, v in verdict["criteria"].items() if not v["passed"]]
        _log(state, "evaluate_report", status, f"{reason} · 미달 기준 {failed or '없음'}")
        summary = {"attempt": attempt, "status": status, "failed_criteria": failed, "feedback": verdict["feedback"],
                   "writer_notes": state.get("writer_notes") or [], "judge": verdict["judge"],
                   "judge_scores": {k: v["judge_score"] for k, v in verdict["criteria"].items() if v["judge_score"]}}
        return {"eval_result": verdict, "eval_feedback": verdict["feedback"], "report_status": status,
                "eval_history": [summary]}
    return evaluate_report


def route_after_eval(state: dict) -> str:
    return "write_report" if state.get("report_status") == "retry" else END


def add_report_nodes(g: StateGraph, llm: Any = None, judge: Any = None, pubmed_cache: Optional[Path] = None,
                     fetch: Optional[Callable] = fetch_pubmed) -> str:
    """그래프에 보고서 노드 3개를 붙이고 시작 노드 이름을 돌려준다. llm·judge="auto"면 환경변수 모델."""
    llm = get_llm() if llm == "auto" else llm
    judge = get_judge() if judge == "auto" else judge
    g.add_node("aggregate", aggregate_node(pubmed_cache, fetch))
    g.add_node("write_report", write_node(llm))
    g.add_node("evaluate_report", evaluate_node(judge))
    g.add_edge("aggregate", "write_report")
    g.add_edge("write_report", "evaluate_report")
    g.add_conditional_edges("evaluate_report", route_after_eval, ["write_report", END])
    return "aggregate"


def build_report_graph(llm: Any = None, judge: Any = None, pubmed_cache: Optional[Path] = None,
                       fetch: Optional[Callable] = fetch_pubmed):
    """보고서만 다시 만들 때 쓰는 단독 그래프 (02~05 미호출)."""
    g = StateGraph(ReportState)
    g.add_edge(START, add_report_nodes(g, llm, judge, pubmed_cache, fetch))
    return g.compile()


def report_check(out: dict) -> dict:
    """그래프 결과 → report_check.json. 기존 키(pages·issues·ok·narrative)는 그대로 두고 evaluation을 더한다."""
    verdict = out["eval_result"]
    history = out.get("eval_history") or []
    return {**verdict["format"],
            "narrative": {"used": sorted(out.get("narrative") or {}),
                          "notes": [f"{h['attempt']}차: {n}" for h in history for n in h["writer_notes"]]},
            "evaluation": {"passed": verdict["passed"], "status": out.get("report_status"), "attempts": len(history),
                           "judge": verdict["judge"],
                           "criteria": {k: {"passed": v["passed"], "judge_score": v["judge_score"],
                                            "issues": [f"{i['target']}: {i['message']}" for i in v["issues"]]}
                                        for k, v in verdict["criteria"].items()},
                           "data_limitations": verdict["warnings"], "history": history}}
