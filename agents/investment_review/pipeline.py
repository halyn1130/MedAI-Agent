"""전체 그래프: Orchestrator(기업별 Worker 실행·판정) → 선정 → 보고서(취합 → 작성 → 품질 평가).

    START ─(기업별 Send)→ company ─→ select ─→ aggregate_results ─→ write_report ─→ review_quality
                                      ↑                                  ↑                │
                                      │                                  └── report 이슈 ──┤ (재작성 ≤ 2회)
                                      └──── orchestrator_review ←── interpretation 이슈 ───┤ (재검토 ≤ 1회)
                                                                                           └→ END

- company            : agents/orchestrator 기업 그래프(build_company_graph) 그대로. 자료 있는 Worker만 실행·보완·판정.
- select             : 적격 기업 중 상위 K개 (Orchestrator select와 같은 규칙, orchestration 기록 유지).
- orchestrator_review: 품질 평가가 'Worker 결과가 이상하다'(interpretation)고 본 기업·Worker만 공통 형식 review_request로
                       다시 실행하고, Orchestrator finalize로 다시 판정한다. 이후 선정부터 다시 하고 보고서를 새로 쓴다.
- agents/orchestrator 코드는 바꾸지 않고 함수(build_company_graph·_payload·_worker_node·_finalize_node)를 가져다 쓴다.

    python -m agents.investment_review.pipeline --input bundles.json --out outputs/pipeline
    python -m agents.investment_review.pipeline --input bundles.json --checkpoint outputs/pipeline/checkpoints.sqlite
    python -m agents.investment_review.pipeline --input bundles.json --checkpoint outputs/pipeline/checkpoints.sqlite \
        --resume <thread_id>            # 중단된 실행 이어서 (끝난 기업·단계는 건너뜀)

State 저장 비용: 기업 입력의 데이터셋 원문은 agents/orchestrator/store.py 파일 저장소로 옮기고 State에는 dataset_ref만 둔다.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, Optional

from langgraph.graph import START, StateGraph
from langgraph.types import Send

from agents.orchestrator.store import dataset_version

from .contract import InvestmentReview
from .policy import DEFAULT_POLICY, POLICIES
from .report.evaluator import issue_company
from .report.nodes import ReportConfig, ReportStageState, _log, _rereview_targets, add_report_nodes
from .selection import select

WorkerFn = Callable[[dict], dict]


def merge(a: Optional[dict], b: Optional[dict]) -> dict:
    return {**(a or {}), **(b or {})}


class PipelineState(ReportStageState, total=False):
    companies: list[dict]                            # [{company_id, company_profile, dataset}, ...] (Orchestrator 입력)
    company_states: Annotated[dict, merge]           # {company_id: Orchestrator 기업 그래프 최종 State}
    worker_log: Annotated[list, operator.add]


def _review_request(cs: dict, worker: str, issues: list[dict], attempt: int) -> dict:
    """common_state.md ReviewRequest (Orchestrator build_review_request와 같은 키). 질문 키는 question·text 둘 다."""
    cid = cs["company_id"]
    prev = (cs.get("worker_results") or {}).get(worker) or {}
    texts = list(dict.fromkeys(q for i in issues for q in i["questions"]))
    return {"request_id": f"{cid}:{worker}:qr{attempt}", "company_id": cid, "target_agent": worker,
            "attempt": attempt, "reason": "interpretation_error", "criterion_id": None, "blocker": False,
            "questions": [{"question_id": f"q{n:02d}", "question": t, "text": t} for n, t in enumerate(texts, 1)],
            "target_finding_ids": [], "related_evidence_ids": [],
            "dataset_version": dataset_version(cs),
            "previous_result_version": prev.get("result_version"), "as_of": cs["as_of"]}


def build_pipeline(workers: Optional[dict[str, WorkerFn]] = None, policy_name: str = "zero_fill",
                   cfg: Optional[ReportConfig] = None, checkpointer: Any = None, **company_kwargs):
    from agents.orchestrator.graph import (_finalize_node, _payload, _worker_node, build_company_graph,
                                           default_workers)
    policy = POLICIES.get(policy_name, DEFAULT_POLICY)
    workers = workers if workers is not None else default_workers()
    cfg = (cfg or ReportConfig(policy_name=policy_name)).resolved()
    cfg.rereview = True
    # 기업 그래프는 노드 안에서 호출하므로 자체 저장은 끄고, 기업 결과는 상위 그래프 체크포인트에 남긴다
    company_graph = build_company_graph(workers, policy, checkpointer=False, **company_kwargs)
    finalize = _finalize_node(policy)

    def fan_out(state: dict):
        base = {k: state.get(k) for k in ("run_id", "as_of", "schema_version", "criteria_version") if state.get(k)}
        return [Send("company", {**base, **c, "company_profile": c.get("company_profile") or {}})
                for c in state.get("companies") or []] or "select"

    def company(state: dict) -> dict:
        out = company_graph.invoke(state)
        return {"company_states": {out["company_id"]: out}}

    def selection(state: dict) -> dict:
        states = state.get("company_states") or {}
        reviews = [cs["investment_review"] for cs in states.values() if cs.get("investment_review")]
        picked = select([InvestmentReview.model_validate(r) for r in reviews], policy, cfg.k)
        orch = {r["company_id"]: r.get("orchestration") for r in reviews}
        return {"final_reviews": [{**r.model_dump(mode="json"), "orchestration": orch.get(r.company_id)}
                                  for r in picked]}

    def orchestrator_review(state: dict) -> dict:
        issues = (state.get("quality_review") or {}).get("issues") or []
        targets = _rereview_targets(issues)
        orch = dict(state.get("orchestration") or {})
        counts = dict(orch.get("retry_counts") or {})
        attempt = counts.get("report_rereview", 0) + 1
        updated, log = {}, []
        for cid, ws in targets.items():
            cs = dict((state.get("company_states") or {}).get(cid) or {})
            if not cs:
                continue
            selected = set((cs.get("plan") or {}).get("selected") or [])
            requested, company_log = {}, []
            for w in ws:
                if w not in selected:      # 실행 대상이 아니었던 Worker(자료 없음)는 재검토할 수 없다
                    continue
                mine = [i for i in issues if i["kind"] == "interpretation" and issue_company(i) == cid
                        and w in i["target_workers"]]
                req = _review_request(cs, w, mine, attempt)
                out = _worker_node(w, workers[w])(_payload(cs, w, req))
                cs["worker_results"] = {**(cs.get("worker_results") or {}), **out["worker_results"]}
                company_log += out["worker_log"]
                requested[w] = req["request_id"]
            if not requested:
                continue
            log += company_log
            cs["worker_log"] = (cs.get("worker_log") or []) + company_log
            cs["review_history"] = (cs.get("review_history") or []) + [
                {"round": f"report-{attempt}", "issue_ids": [i["issue_id"] for i in issues if issue_company(i) == cid],
                 "requested": requested, "stopped_by_cap": False}]
            cs.update(finalize(cs))                                       # Orchestrator finalize로 다시 판정
            updated[cid] = cs
        counts["report_rereview"] = attempt
        _log(state, "orchestrator_review", "rereview",
             f"{attempt}차 재검토 · " + ", ".join(f"{c}:{'/'.join(ws)}" for c, ws in targets.items()))
        # Worker 결과가 바뀌었으므로 후속 취합·보고서·품질 평가를 비우고 다시 만든다 (common_state.md 7-5)
        return {"company_states": updated, "worker_log": log,
                "orchestration": {**orch, "retry_counts": counts},
                "aggregated_result": None, "report": None, "quality_review": None, "report_draft": {}}

    g = StateGraph(PipelineState)
    g.add_node("company", company)
    g.add_node("select", selection)
    g.add_node("orchestrator_review", orchestrator_review)
    add_report_nodes(g, cfg)
    g.add_conditional_edges(START, fan_out, ["company", "select"])
    g.add_edge("company", "select")
    g.add_edge("select", "aggregate_results")
    g.add_edge("orchestrator_review", "select")
    return g.compile(checkpointer=checkpointer)


def run_pipeline(companies: list[dict], run_id: str, as_of: str, cfg: ReportConfig,
                 workers: Optional[dict[str, WorkerFn]] = None, max_concurrency: int = 5,
                 checkpointer: Any = None, thread_id: Optional[str] = None, **company_kwargs) -> dict:
    """checkpointer와 thread_id를 주면 단계마다 저장하고, 같은 thread_id로 다시 부르면 멈춘 곳부터 이어서 실행한다."""
    from agents.orchestrator.store import externalize, invoke_resumable
    graph = build_pipeline(workers, cfg.policy_name, cfg, checkpointer=checkpointer, **company_kwargs)
    config: dict = {"max_concurrency": max_concurrency, "run_name": "investment_pipeline",
                    "metadata": {"run_id": run_id, "thread_id": thread_id}}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
    return invoke_resumable(graph, {"run_id": run_id, "as_of": as_of, "companies": externalize(companies),
                                    "orchestration": {"retry_counts": {"report_rereview": 0}}}, config)


def main() -> None:
    import argparse
    import json
    import logging
    from datetime import date
    from pathlib import Path

    from dotenv import load_dotenv

    from agents.orchestrator.__main__ import load_bundles
    from agents.orchestrator.routing import WORKERS

    from .report.__main__ import save_report
    from .report.nodes import report_check, report_context

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, nargs="+", required=True, help="Orchestrator 입력 (기업별 dataset 포함 JSON)")
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--out", type=Path, default=Path("outputs/pipeline"))
    ap.add_argument("--k", type=int)
    ap.add_argument("--policy", choices=POLICIES, default="zero_fill")
    ap.add_argument("--max-review-rounds", type=int, default=1, help="기업당 Orchestrator 보완 라운드 상한")
    ap.add_argument("--workers", nargs="*", choices=WORKERS, default=list(WORKERS))
    ap.add_argument("--max-concurrency", type=int, default=5)
    ap.add_argument("--no-llm-report", action="store_true", help="보고서 LLM 서술 없이 템플릿 문장만")
    ap.add_argument("--no-judge", action="store_true", help="품질 평가에서 LLM Judge 생략 (규칙 검사만)")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--checkpoint", type=Path, help="체크포인트 SQLite 파일 (지정하면 단계마다 저장 → 재개 가능)")
    ap.add_argument("--resume", metavar="THREAD_ID", help="이 thread_id의 중단된 실행을 이어서 (--checkpoint 필요)")
    args = ap.parse_args()
    if args.resume and not args.checkpoint:
        ap.error("--resume 은 --checkpoint 와 함께 쓰세요")
    load_dotenv()

    args.out.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(args.out / "decision_log.jsonl", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("investment_review.report").addHandler(handler)
    logging.getLogger("investment_review.report").setLevel(logging.INFO)

    import uuid

    from agents.orchestrator.store import make_checkpointer
    run_id = f"pipeline-{args.as_of}"
    thread_id = args.resume or (f"{run_id}-{uuid.uuid4().hex[:8]}" if args.checkpoint else None)
    if thread_id:
        print(f"체크포인트: {args.checkpoint} · thread_id={thread_id} (중단되면 --resume {thread_id})")
    cfg = ReportConfig(policy_name=args.policy, k=args.k, llm=None if args.no_llm_report else "auto",
                       judge=None if args.no_judge else "auto", pubmed_cache=args.out / "pubmed_cache.json")
    out = run_pipeline(load_bundles(args.input), run_id, args.as_of, cfg, max_concurrency=args.max_concurrency,
                       checkpointer=make_checkpointer(args.checkpoint), thread_id=thread_id,
                       max_review_rounds=args.max_review_rounds, enabled=args.workers)

    (args.out / "final_reviews.json").write_text(json.dumps(out["final_reviews"], ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    (args.out / "worker_results.json").write_text(json.dumps(
        {cid: {"plan": cs.get("plan"), "worker_results": cs.get("worker_results")}
         for cid, cs in out["company_states"].items()}, ensure_ascii=False), encoding="utf-8")
    check = save_report(args.out, out["report"]["content"], report_check(out), report_context(out, cfg),
                        pdf=not args.no_pdf)
    ev = check["evaluation"]
    for r in out["final_reviews"]:
        total = "-" if r["total_score"] is None else f"{r['total_score']:.1f}"
        print(f"{r['company_id']} {r['final_status']:12} {total:>6}  {r['selection']['reason']}")
    print(f"\n보고서: {args.out / 'final_report.md'} · 품질 평가 {ev['status']} ({ev['attempts']}회 작성, "
          f"재검토 {ev['rereviews']}회, 종료 사유 {ev['termination_reason']})")


if __name__ == "__main__":
    main()
