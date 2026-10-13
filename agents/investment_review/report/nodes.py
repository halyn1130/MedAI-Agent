"""보고서 노드 3개: 결과 취합 → 보고서 작성 → 품질 평가. 공통 State(common_state.md)와 Orchestrator(agents/orchestrator) 출력 기준.

| 노드              | 읽기                                                  | 쓰기                                  | 다음 경로 |
|-------------------|-------------------------------------------------------|---------------------------------------|-----------|
| aggregate_results | final_reviews(Orchestrator 판정·선정), company_states | aggregated_result                     | write_report |
| write_report      | aggregated_result, company_states, quality_review     | report (+ report_draft)               | review_quality |
| review_quality    | report, aggregated_result, company_states             | quality_review (+ quality_detail)     | route_after_quality |

route_after_quality (품질 평가 노드가 결정)
- pass                                         → END
- interpretation(Worker 결과 이상) & 재검토 < 상한 → orchestrator_review (해당 기업·Worker 재검토, pipeline.py)
- report(서술 문제) & 재작성 < 상한             → write_report (반려된 항목만 다시 씀)
- 그 외                                        → END (사유는 quality_detail.termination_reason)
데이터 부족은 취합 노드가 판단해 aggregated_result.unresolved_items에 남기고 보고서 4장에 공개한다 (수집 노드 미구현).

입력
- final_reviews : Orchestrator select 결과 (기업별 InvestmentReview + orchestration). 판정은 다시 계산하지 않는다.
- company_states: {company_id: Orchestrator 기업 그래프 최종 State} (company_profile·dataset·plan·worker_results·review_history)

State 설계
- 공통 칸 aggregated_result·report·quality_review는 common_state.md 형식 그대로.
- 노드 내부 작업값(공통 계약 밖): report_draft {attempt, narrative, notes}, quality_detail {format, criteria, judge, next,
  termination_reason, history}. 반려된 항목만 다시 쓰려면 이전 문장이 필요하다.
- 보고서 재료(report_context)는 State에 두지 않고 노드마다 다시 만든다 (체크포인트 크기).
- 결정·사유는 logger(JSON)로 남기고 run_id로 외부 로그와 잇는다.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from ..policy import CRITERIA_VERSION, DEFAULT_K, SCHEMA_VERSION
from .checker import check_narrative
from .citations import fetch_pubmed, pubmed_metadata
from .context_builder import build_context
from .evaluator import (QUESTION, data_limitations, evaluate, get_judge, issue_company, note_key, quality_status,
                        to_quality_issues)
from .renderer import render
from .writer import get_llm, write

WORKERS = ("clinical", "market", "traction", "risk")
REPORT_REVISION_LIMIT = 2      # 재작성 상한 (최초 작성 제외) → 최대 3회 작성
REREVIEW_LIMIT = 1             # 보고서 평가 → Orchestrator 재검토 상한
log = logging.getLogger("investment_review.report")


class ReportStageState(TypedDict, total=False):
    # ── 공통 메타데이터 ──
    run_id: str
    as_of: str
    schema_version: str
    criteria_version: str
    # ── 입력 (Orchestrator 출력) ──
    final_reviews: list
    company_states: dict
    # ── 공통 칸 ──
    aggregated_result: Optional[dict]
    report: Optional[dict]
    quality_review: Optional[dict]
    orchestration: dict                          # 재검토 횟수 (orchestrator_review가 갱신)
    # ── 보고서 노드 내부 작업값 ──
    report_draft: dict
    quality_detail: dict


@dataclass
class ReportConfig:
    """실행 설정. State가 아니라 노드 생성 시 넘긴다."""
    policy_name: str = "zero_fill"
    k: Optional[int] = None
    llm: Any = None                              # 보고서 작성 모델. None=템플릿, "auto"=REPORT_MODEL
    judge: Any = None                            # 품질 평가 Judge. None=규칙만, "auto"=REPORT_JUDGE_MODEL
    pubmed_cache: Optional[Path] = None
    fetch: Optional[Callable] = fetch_pubmed
    rereview: bool = False                       # interpretation → Orchestrator 재검토 경로 사용 (pipeline.py)
    revision_limit: int = REPORT_REVISION_LIMIT
    rereview_limit: int = REREVIEW_LIMIT

    def resolved(self) -> "ReportConfig":
        from dataclasses import replace
        return replace(self, llm=get_llm() if self.llm == "auto" else self.llm,
                       judge=get_judge() if self.judge == "auto" else self.judge)


def _log(state: dict, node: str, decision: str, reason: str) -> None:
    log.info(json.dumps({"run_id": state.get("run_id"), "node": node,
                         "attempt": (state.get("report_draft") or {}).get("attempt"),
                         "decision": decision, "reason": reason}, ensure_ascii=False))


# ── 보고서 재료 (State에 저장하지 않고 매번 만든다) ─────────────────────────────
def report_context(state: dict, cfg: ReportConfig, fetch: Optional[Callable] = None,
                   reviews: Optional[list] = None) -> dict:
    states = state.get("company_states") or {}
    analyses = {}
    for cid, cs in states.items():
        wr = cs.get("worker_results") or {}
        analyses[cid] = {f"{w}_analysis": wr[w] for w in WORKERS if wr.get(w)} | {
            "references": (wr.get("risk") or {}).get("sources") or []}
    profiles = {cid: cs.get("company_profile") or {} for cid, cs in states.items()}
    meta = {"as_of": state["as_of"], "criteria_version": state.get("criteria_version") or CRITERIA_VERSION,
            "policy": cfg.policy_name, "k": cfg.k or DEFAULT_K}
    if reviews is None:
        reviews = state["aggregated_result"]["criteria_results"]["reviews"]
    ctx = build_context(reviews, analyses, profiles, meta)
    ctx["pubmed"] = pubmed_metadata(ctx["references"], cfg.pubmed_cache, fetch or (lambda _ids: {}))
    ctx["limitations"] = data_limitations(ctx)
    return ctx


# ── 취합 ────────────────────────────────────────────────────────────────────
def based_on_results(company_states: dict) -> dict:
    return {cid: {w: (r or {}).get("result_version") or 1 for w, r in (cs.get("worker_results") or {}).items()}
            for cid, cs in company_states.items()}


def _required_missing(cid: str, cs: dict) -> list[dict]:
    out = []
    for w, res in (cs.get("worker_results") or {}).items():
        for m in (res or {}).get("missing_items") or []:
            if m.get("required_for_assessment") or ("item" not in m and m.get("impact") in ("high", "blocking")):
                out.append({"kind": "missing_item", "company_id": cid, "worker": w,
                            "missing_id": m.get("missing_id") or m.get("item_id"),
                            "item": m.get("item") or m.get("field"), "reason": m.get("reason") or m.get("cause")})
    return out


def _analysis_status(company_states: dict) -> str:
    statuses = [((cs.get("worker_results") or {}).get(w) or {}).get("analysis_status")
                for cs in company_states.values() for w in WORKERS]
    if not statuses or all(s in (None, "failed") for s in statuses):
        return "failed"
    return "complete" if all(s == "complete" for s in statuses) else "partial"


def aggregate_node(cfg: ReportConfig):
    """판정·선정은 Orchestrator 결과(final_reviews)를 그대로 쓴다. 데이터 부족은 여기서 판단해 unresolved_items에 남긴다."""
    def aggregate_results(state: dict) -> dict:
        reviews = state.get("final_reviews") or []
        states = state.get("company_states") or {}
        ctx = report_context(state, cfg, reviews=reviews)
        names = {c["company_id"]: c["name"] for c in ctx["selected"]}
        data_gaps = [{"kind": "data_limitation", "company_id": lim["company_id"], "criterion": lim["criterion"],
                      "workers": lim["workers"], "text": lim["text"],
                      "question": QUESTION[lim["criterion"]].format(name=names.get(lim["company_id"], lim["company_id"]))}
                     for lim in ctx["limitations"]]
        status = Counter(r["final_status"] for r in reviews)
        summary = (f"후보 {len(reviews)}개 · 적격 {status.get('eligible', 0)} · 부적격 {status.get('ineligible', 0)} · "
                   f"판단불가 {status.get('undetermined', 0)} · 선정 {len(ctx['selected'])}개")
        result = {"based_on_results": based_on_results(states), "summary": summary,
                  "criteria_results": {"reviews": reviews},
                  "unresolved_items": data_gaps + [u for cid, cs in states.items() for u in _required_missing(cid, cs)],
                  "analysis_status": _analysis_status(states)}
        _log(state, "aggregate_results", "data_gaps" if data_gaps else "sufficient",
             f"{summary} · 데이터 부족 {len(data_gaps)}건 (수집 노드 없음 → 보고서 4장에 공개)")
        return {"aggregated_result": result}
    return aggregate_results


# ── 보고서 작성 ─────────────────────────────────────────────────────────────
def write_node(cfg: ReportConfig):
    """quality_review에 kind=report 이슈가 있으면 그 항목만 다시 쓰고, 통과한 항목은 유지한다."""
    def write_report(state: dict) -> dict:
        ctx = report_context(state, cfg, cfg.fetch)
        draft = state.get("report_draft") or {}
        prev = dict(draft.get("narrative") or {})
        feedback = [i["description"] for i in (state.get("quality_review") or {}).get("issues") or []
                    if i["kind"] == "report"]
        narrative, notes = prev, []
        if cfg.llm is not None:
            raw = write(ctx, cfg.llm, feedback=feedback or None)
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
        attempt = draft.get("attempt", 0) + 1
        _log(state, "write_report", "written", f"{attempt}차 작성 · 반영한 반려 {len(feedback)}건 · 서술 {len(narrative)}개")
        return {"report": {"based_on_results": state["aggregated_result"]["based_on_results"],
                           "content": render(ctx, narrative)},
                "report_draft": {"attempt": attempt, "narrative": narrative, "notes": notes}}
    return write_report


# ── 품질 평가 ───────────────────────────────────────────────────────────────
def _rereview_targets(issues: list[dict]) -> dict[str, list[str]]:
    """interpretation 이슈 → {company_id: [worker]} (기업·Worker가 지정된 것만)."""
    out: dict[str, list[str]] = {}
    for i in issues:
        if i["kind"] == "interpretation" and (cid := issue_company(i)):
            out.setdefault(cid, [])
            out[cid] += [w for w in i["target_workers"] if w not in out[cid]]
    return {cid: ws for cid, ws in out.items() if ws}


def _next_step(state: dict, cfg: ReportConfig, issues: list[dict], status: str) -> tuple[str, Optional[str]]:
    revisions = (state.get("report_draft") or {}).get("attempt", 1) - 1
    rereviews = ((state.get("orchestration") or {}).get("retry_counts") or {}).get("report_rereview", 0)
    has_report = any(i["kind"] == "report" for i in issues)
    if status == "pass":
        return "finish", None
    if cfg.rereview and _rereview_targets(issues) and rereviews < cfg.rereview_limit:
        return "orchestrator_review", None       # Worker 결과가 바뀌면 보고서는 새로 쓰므로 서술 수정보다 먼저
    if has_report and revisions < cfg.revision_limit:
        return "write_report", None
    if has_report:
        return "finish", "report_revision_limit"
    if _rereview_targets(issues):
        return "finish", "rereview_limit" if cfg.rereview else "rereview_unavailable"
    return "finish", "unresolved_non_report_issues"


def quality_node(cfg: ReportConfig):
    def review_quality(state: dict) -> dict:
        ctx = report_context(state, cfg)                       # PubMed는 작성 노드가 만든 캐시만 사용
        draft = state.get("report_draft") or {}
        verdict = evaluate(state["report"]["content"], ctx, draft.get("narrative") or {}, draft.get("notes") or [],
                           cfg.judge)
        issues = to_quality_issues(verdict, ctx)
        status = quality_status(issues)
        step, reason = _next_step(state, cfg, issues, status)
        kinds = dict(Counter(i["kind"] for i in issues))
        _log(state, "review_quality", step, f"quality_review={status} · 이슈 {kinds or '없음'}" + (f" · {reason}" if reason else ""))
        detail = state.get("quality_detail") or {}
        summary = {"attempt": draft.get("attempt"), "status": status, "next": step, "issue_kinds": kinds,
                   "failed_criteria": [k for k, v in verdict["criteria"].items() if not v["passed"]],
                   "report_issues": [i["description"] for i in issues if i["kind"] == "report"],
                   "rereview_targets": _rereview_targets(issues),
                   "writer_notes": draft.get("notes") or [], "judge": verdict["judge"],
                   "judge_scores": {k: v["judge_score"] for k, v in verdict["criteria"].items() if v["judge_score"]}}
        return {"quality_review": {"based_on_results": state["report"]["based_on_results"], "status": status,
                                   "issues": issues},
                "quality_detail": {"format": verdict["format"], "criteria": verdict["criteria"], "judge": verdict["judge"],
                                   "next": step, "termination_reason": reason,
                                   "history": (detail.get("history") or []) + [summary]}}
    return review_quality


def route_after_quality(state: dict) -> str:
    step = (state.get("quality_detail") or {}).get("next")
    return step if step in ("write_report", "orchestrator_review") else END


def add_report_nodes(g: StateGraph, cfg: ReportConfig) -> None:
    """aggregate_results → write_report → review_quality → (write_report | orchestrator_review | END).
    cfg.rereview=True면 그래프에 orchestrator_review 노드가 있어야 한다 (pipeline.py)."""
    g.add_node("aggregate_results", aggregate_node(cfg))
    g.add_node("write_report", write_node(cfg))
    g.add_node("review_quality", quality_node(cfg))
    g.add_edge("aggregate_results", "write_report")
    g.add_edge("write_report", "review_quality")
    targets = ["write_report", END] + (["orchestrator_review"] if cfg.rereview else [])
    g.add_conditional_edges("review_quality", route_after_quality, targets)


# ── 입력 변환·결과 정리 ─────────────────────────────────────────────────────
def company_states_from_analyses(analyses: dict[str, dict], companies: dict[str, dict], reviews: list[dict],
                                 run_id: str, as_of: str) -> dict[str, dict]:
    """기존 실행 결과(기업별 *_analysis, risk references) → {company_id: Orchestrator 기업 State 형태}."""
    out = {}
    for cid in [r["company_id"] for r in reviews] or list(companies):
        a = analyses.get(cid) or {}
        wr = {w: a[f"{w}_analysis"] for w in ("clinical", "market", "traction") if a.get(f"{w}_analysis")}
        if a.get("risk_analysis"):
            wr["risk"] = {**a["risk_analysis"], "sources": a.get("references") or []}
        out[cid] = {"run_id": run_id, "company_id": cid, "as_of": as_of, "schema_version": SCHEMA_VERSION,
                    "criteria_version": CRITERIA_VERSION, "company_profile": companies.get(cid) or {"company_id": cid},
                    "worker_results": wr}
    return out


def report_check(out: dict) -> dict:
    """최종 State → report_check.json. 기존 키(pages·issues·ok·narrative)는 그대로 두고 evaluation을 더한다."""
    detail, review = out["quality_detail"], out["quality_review"]
    history = detail.get("history") or []
    return {**detail["format"],
            "narrative": {"used": sorted((out.get("report_draft") or {}).get("narrative") or {}),
                          "notes": [f"{h['attempt']}차: {n}" for h in history for n in h["writer_notes"]]},
            "evaluation": {"status": review["status"], "passed": review["status"] == "pass", "attempts": len(history),
                           "termination_reason": detail.get("termination_reason"),
                           "rereviews": ((out.get("orchestration") or {}).get("retry_counts") or {}).get("report_rereview", 0),
                           "judge": detail["judge"],
                           "criteria": {k: {"passed": v["passed"], "judge_score": v["judge_score"],
                                            "issues": [f"{i['target']}: {i['message']}" for i in v["issues"]]}
                                        for k, v in detail["criteria"].items()},
                           "quality_issues": review["issues"],
                           "data_gaps": [u for u in (out.get("aggregated_result") or {}).get("unresolved_items") or []
                                         if u["kind"] == "data_limitation"],
                           "history": history}}
