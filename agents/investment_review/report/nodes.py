"""보고서 노드 3개: 결과 취합 → 보고서 작성 → 품질 평가. 공통 State(common_state.md) 기준.

각 노드는 공통 State의 자기 칸만 쓰고, 다음 경로·반복 횟수는 정하지 않는다 (Orchestrator 담당).

| 노드            | 읽기                                              | 쓰기                                  |
|-----------------|---------------------------------------------------|---------------------------------------|
| aggregate_results | evaluations[].worker_results·company_profile·orchestration | aggregated_result                     |
| write_report    | aggregated_result, evaluations, quality_review(재작성 시) | report (+ report_draft)               |
| review_quality  | report, aggregated_result, evaluations            | quality_review (+ quality_detail)     |

보고서는 여러 기업을 비교하므로 입력은 기업별 EvaluationState 목록(evaluations)이다. 기업 1개 실행 State는 바꾸지 않는다.

State 설계
- 공통 칸: aggregated_result·report·quality_review는 common_state.md 형식 그대로 쓴다.
- 노드 내부 작업값(공통 계약 밖, 보고서 노드만 읽고 씀):
  report_draft   = {attempt, narrative, notes}  반려된 항목만 다시 쓰려면 이전 문장이 필요하다.
  quality_detail = {format, criteria, judge, history}  기준별 점수·Judge 상태·시도 이력 (report_check.json용).
- 보고서 재료(report_context)는 State에 두지 않고 노드마다 aggregated_result + evaluations로 다시 만든다 (체크포인트 크기).
- 결정·사유는 logger(JSON)로 남기고 run_id로 외부 로그와 잇는다.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from ..contract import InvestmentReview
from ..finalize import WORKERS, adapt_worker_results, finalize_review
from ..policy import CRITERIA_VERSION, DEFAULT_K, POLICIES, SCHEMA_VERSION
from ..selection import select
from .checker import check_narrative
from .citations import fetch_pubmed, pubmed_metadata
from .context_builder import build_context
from .evaluator import data_limitations, evaluate, get_judge, note_key, quality_status, to_quality_issues
from .renderer import render
from .writer import get_llm, write

log = logging.getLogger("investment_review.report")


class ReportStageState(TypedDict, total=False):
    # ── 공통 메타데이터 (EvaluationState와 같은 이름·의미) ──
    run_id: str
    as_of: str
    schema_version: str
    criteria_version: str
    # ── 입력: 기업별 EvaluationState (company_id·company_profile·worker_results·orchestration) ──
    evaluations: list[dict]
    # ── 공통 칸 ──
    aggregated_result: Optional[dict]
    report: Optional[dict]
    quality_review: Optional[dict]
    orchestration: dict                          # Orchestrator만 갱신 (지금은 standalone.py의 임시 Orchestrator)
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

    def resolved(self) -> "ReportConfig":
        return ReportConfig(self.policy_name, self.k, get_llm() if self.llm == "auto" else self.llm,
                            get_judge() if self.judge == "auto" else self.judge, self.pubmed_cache, self.fetch)


def _log(state: dict, node: str, decision: str, reason: str) -> None:
    log.info(json.dumps({"run_id": state.get("run_id"), "node": node,
                         "attempt": (state.get("report_draft") or {}).get("attempt"),
                         "decision": decision, "reason": reason}, ensure_ascii=False))


# ── 취합 ────────────────────────────────────────────────────────────────────
def _request_ids(ev: dict) -> list[str]:
    """보완 요청 이력 = orchestration.retry_counts.worker_reviews (Worker별 재검토 횟수)."""
    counts = ((ev.get("orchestration") or {}).get("retry_counts") or {}).get("worker_reviews") or {}
    return [f"{ev['company_id']}:{w}:rr{n}" for w in WORKERS for n in range(1, (counts.get(w) or 0) + 1)]


def _unresolved(ev: dict) -> list[dict]:
    out = []
    for w, res in (ev.get("worker_results") or {}).items():
        for m in (res or {}).get("missing_items") or []:
            if m.get("required_for_assessment") or ("item" not in m and m.get("impact") in ("high", "blocking")):
                out.append({"company_id": ev["company_id"], "worker": w,
                            "missing_id": m.get("missing_id") or m.get("item_id"),
                            "item": m.get("item") or m.get("field"), "reason": m.get("reason") or m.get("cause")})
    return out


def based_on_results(evaluations: list[dict]) -> dict:
    return {ev["company_id"]: {w: (r or {}).get("result_version") or 1 for w, r in (ev.get("worker_results") or {}).items()}
            for ev in evaluations}


def _analysis_status(evaluations: list[dict]) -> str:
    statuses = [(ev.get("worker_results") or {}).get(w, {}).get("analysis_status") if (ev.get("worker_results") or {}).get(w)
                else None for ev in evaluations for w in WORKERS]
    if not statuses or all(s in (None, "failed") for s in statuses):
        return "failed"
    return "complete" if all(s == "complete" for s in statuses) else "partial"


def aggregate_node(cfg: ReportConfig):
    policy = POLICIES[cfg.policy_name]

    def aggregate_results(state: dict) -> dict:
        evaluations = state.get("evaluations") or []
        reviews = []
        for ev in evaluations:
            cid, wr = ev["company_id"], ev.get("worker_results") or {}
            results = adapt_worker_results(cid, wr)
            reviews.append(finalize_review(cid, state["as_of"], results, wr, _request_ids(ev), policy))
        ranked = select(reviews, policy, cfg.k)
        status = Counter(r.final_status.value for r in ranked)
        selected = sum(r.selection.selected for r in ranked)
        summary = (f"후보 {len(ranked)}개 · 적격 {status.get('eligible', 0)} · 부적격 {status.get('ineligible', 0)} · "
                   f"판단불가 {status.get('undetermined', 0)} · 선정 {selected}개")
        result = {"based_on_results": based_on_results(evaluations), "summary": summary,
                  "criteria_results": {"reviews": [r.model_dump(mode="json") for r in ranked]},
                  "unresolved_items": [u for ev in evaluations for u in _unresolved(ev)],
                  "analysis_status": _analysis_status(evaluations)}
        _log(state, "aggregate_results", result["analysis_status"], summary)
        return {"aggregated_result": result}
    return aggregate_results


# ── 보고서 재료 (State에 저장하지 않고 매번 만든다) ─────────────────────────────
def report_context(state: dict, cfg: ReportConfig, fetch: Optional[Callable] = None) -> dict:
    evaluations = state.get("evaluations") or []
    analyses = {}
    for ev in evaluations:
        wr = ev.get("worker_results") or {}
        analyses[ev["company_id"]] = {f"{w}_analysis": wr[w] for w in WORKERS if wr.get(w)} | {
            "references": (wr.get("risk") or {}).get("sources") or []}
    profiles = {ev["company_id"]: ev.get("company_profile") or {} for ev in evaluations}
    meta = {"as_of": state["as_of"], "criteria_version": state.get("criteria_version") or CRITERIA_VERSION,
            "policy": cfg.policy_name, "k": cfg.k or DEFAULT_K}
    ctx = build_context(state["aggregated_result"]["criteria_results"]["reviews"], analyses, profiles, meta)
    ctx["pubmed"] = pubmed_metadata(ctx["references"], cfg.pubmed_cache, fetch or (lambda _ids: {}))
    ctx["limitations"] = data_limitations(ctx)
    return ctx


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
def quality_node(cfg: ReportConfig):
    def review_quality(state: dict) -> dict:
        ctx = report_context(state, cfg)                       # PubMed는 작성 노드가 만든 캐시만 사용
        draft = state.get("report_draft") or {}
        verdict = evaluate(state["report"]["content"], ctx, draft.get("narrative") or {}, draft.get("notes") or [],
                           cfg.judge)
        issues = to_quality_issues(verdict, ctx)
        status = quality_status(issues)
        kinds = dict(Counter(i["kind"] for i in issues))
        _log(state, "review_quality", status, f"이슈 {kinds or '없음'}")
        detail = state.get("quality_detail") or {}
        summary = {"attempt": draft.get("attempt"), "status": status, "issue_kinds": kinds,
                   "failed_criteria": [k for k, v in verdict["criteria"].items() if not v["passed"]],
                   "report_issues": [i["description"] for i in issues if i["kind"] == "report"],
                   "writer_notes": draft.get("notes") or [], "judge": verdict["judge"],
                   "judge_scores": {k: v["judge_score"] for k, v in verdict["criteria"].items() if v["judge_score"]}}
        return {"quality_review": {"based_on_results": state["report"]["based_on_results"], "status": status,
                                   "issues": issues},
                "quality_detail": {"format": verdict["format"], "criteria": verdict["criteria"], "judge": verdict["judge"],
                                   "history": (detail.get("history") or []) + [summary]}}
    return review_quality


# ── 입력 변환·결과 정리 ─────────────────────────────────────────────────────
def evaluations_from_analyses(analyses: dict[str, dict], companies: dict[str, dict], reviews: list[dict],
                              run_id: str, as_of: str) -> list[dict]:
    """기존 실행 결과(기업별 *_analysis, risk references, final_reviews) → 기업별 EvaluationState.
    risk 출처는 결과 안 sources로, 보완 요청 이력은 orchestration.retry_counts.worker_reviews로 옮긴다."""
    ids = [r["company_id"] for r in reviews] or list(companies)
    requests = {r["company_id"]: r.get("review_request_ids") or [] for r in reviews}
    out = []
    for cid in ids:
        a = analyses.get(cid) or {}
        wr = {w: a[f"{w}_analysis"] for w in ("clinical", "market", "traction") if a.get(f"{w}_analysis")}
        if a.get("risk_analysis"):
            wr["risk"] = {**a["risk_analysis"], "sources": a.get("references") or []}
        out.append({"run_id": run_id, "company_id": cid, "as_of": as_of, "schema_version": SCHEMA_VERSION,
                    "criteria_version": CRITERIA_VERSION, "company_profile": companies.get(cid) or {"company_id": cid},
                    "worker_results": wr,
                    "orchestration": {"retry_counts": {"worker_reviews": dict(Counter(i.split(":")[1] for i in requests.get(cid, [])))}}})
    return out


def report_check(out: dict) -> dict:
    """최종 State → report_check.json. 기존 키(pages·issues·ok·narrative)는 그대로 두고 evaluation을 더한다."""
    detail, review = out["quality_detail"], out["quality_review"]
    history = detail.get("history") or []
    orch = out.get("orchestration") or {}
    return {**detail["format"],
            "narrative": {"used": sorted((out.get("report_draft") or {}).get("narrative") or {}),
                          "notes": [f"{h['attempt']}차: {n}" for h in history for n in h["writer_notes"]]},
            "evaluation": {"status": review["status"], "passed": review["status"] == "pass", "attempts": len(history),
                           "termination_reason": orch.get("termination_reason"), "judge": detail["judge"],
                           "criteria": {k: {"passed": v["passed"], "judge_score": v["judge_score"],
                                            "issues": [f"{i['target']}: {i['message']}" for i in v["issues"]]}
                                        for k, v in detail["criteria"].items()},
                           "quality_issues": review["issues"], "history": history}}
