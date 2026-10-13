"""Orchestrator-Workers 메인 그래프.

기업 그래프 (기업 1곳)
    START → orchestrator ─(자료 있는 Worker만 Send)→ clinical / market / traction / risk ─→ quality_check
                │                                                                          │
                └──(실행할 Worker 없음)──────────────────────→ finalize ←──(보완 요청 없음 / 상한 도달)┘
                                                                  ↑
                         quality_check ─(보완 요청 있음)→ 요청받은 Worker만 다시 Send → quality_check

전체 그래프 (기업 N곳)
    START ─(기업별 Send)→ company(기업 그래프) → select → END

역할 분담
- orchestrator  : 데이터셋을 보고 실행할 Worker를 고른다(routing.plan_workers). 고른 Worker에는 그 Worker 자료만 잘라 보낸다.
- Worker 4개    : 받은 자료로 한 번 평가해 WorkerResult를 반환한다. 재시도·재수집·건너뛰기 판단 없음.
- quality_check : 결과를 교차 검증하고(investment_review.validate), 보완이 필요하면 공통 형식 review_request를 만든다.
                  재실행 여부·횟수 상한(max_review_rounds)은 여기서만 결정한다.
- finalize      : 채점·게이트·판정(investment_review.judge). 실행하지 않은 Worker는 not_run으로 넘기고 사유를 남긴다.

State 설계 (노션 C. State Schema)
- 제어 vs 페이로드: ControlState(라우팅·루프 판단용 작은 값)와 PayloadState(작업 입력·결과)를 타입으로 나눈다.
  orchestrator·quality_check의 분기 판단은 제어 값(plan·review_round·review_requests)만 보고 한다.
- 지속성 비용: 데이터셋 원문은 State에 두지 않고 store.put_dataset으로 파일에 저장, State에는 dataset_ref만 둔다.
- 재개/복구: build_company_graph·build_graph에 checkpointer를 넘기면 단계마다 State가 저장되고,
  같은 thread_id로 다시 실행하면 끝난 단계는 건너뛴다 (store.invoke_resumable).
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, Iterable, Optional, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agents.investment_review.adapters import adapt_clinical, adapt_market, adapt_risk, adapt_traction, not_run
from agents.investment_review.contract import AgentResult, InvestmentReview, ReportDraft
from agents.investment_review.judge import judge
from agents.investment_review.policy import CRITERIA_VERSION, DEFAULT_POLICY, SCHEMA_VERSION, Policy
from agents.investment_review.review_requests import REASON_PRIORITY, _question
from agents.investment_review.selection import select
from agents.investment_review.validate import validate

from .routing import WORKER_KO, WORKERS, plan_workers, slice_dataset
from .store import dataset_version, load_dataset, put_dataset

WorkerFn = Callable[[dict], dict]
MAX_REVIEW_ROUNDS = 1       # 기업당 보완 라운드 상한 (Orchestrator가 소유)


def merge(a: Optional[dict], b: Optional[dict]) -> dict:
    """worker_results 리듀서: 병렬 Worker가 각자 자기 키만 반환하므로 dict를 합친다."""
    return {**(a or {}), **(b or {})}


class _CompanyKeys(TypedDict):
    company_id: str                  # 필수: 심사 대상
    as_of: str                       # 필수: 심사 기준일


class ControlState(_CompanyKeys, total=False):
    """제어: 라우팅·계획·루프 판단에 쓰는 최소 상태. 작고, 단계마다 저장돼도 부담이 없다."""
    run_id: str                                   # 외부 로그(LangSmith metadata·결정 로그)와 잇는 키
    schema_version: str
    criteria_version: str
    plan: dict                                    # {selected, skipped(사유), record_counts, unassigned, rule}
    review_requests: dict                         # 이번 라운드 보완 요청 {worker: request} (없으면 {})
    review_round: int                             # 보완 라운드 수 (상한 max_review_rounds)
    review_history: Annotated[list, operator.add]  # 라운드별 이슈·요청·상한 도달 여부
    worker_log: Annotated[list, operator.add]     # Worker 실행 기록 (상태·레코드 수·요청 ID)


class PayloadState(TypedDict, total=False):
    """페이로드: 작업 입력과 결과. 분기 판단에는 쓰지 않는다."""
    company_profile: dict
    dataset_ref: dict                             # 데이터셋 원문 참조 {version, uri, sha, records, sources}
    dataset: Optional[dict]                       # 인라인 원문(테스트·하위 호환). orchestrator가 참조로 바꾸고 비운다
    worker_results: Annotated[dict, merge]        # {worker: WorkerResult}, 병렬 쓰기는 merge 리듀서로 합침
    investment_review: dict                       # 최종 판정


class CompanyState(ControlState, PayloadState, total=False):
    pass


CONTROL_KEYS = tuple(k for k in ControlState.__annotations__ if k not in _CompanyKeys.__annotations__)
PAYLOAD_KEYS = tuple(PayloadState.__annotations__)


class RunState(TypedDict, total=False):
    run_id: str
    as_of: str
    schema_version: str
    criteria_version: str
    companies: list[dict]        # [{company_id, company_profile, dataset_ref(또는 dataset)}, ...]
    reviews: Annotated[list, operator.add]
    analyses: Annotated[list, operator.add]
    final_reviews: list


# ── Worker 호출 ────────────────────────────────────────────────────────────
def default_workers() -> dict[str, WorkerFn]:
    """팀원 Worker 노드. import가 무거워 그래프를 만들 때만 불러온다."""
    from agents.clinical_regulatory.agent import clinical_analysis_node
    from agents.market.agent import market_agent_node
    from agents.risk.agent import risk_worker_node
    from agents.traction_growth.evaluator import evaluate_worker

    def traction(worker_input: dict) -> dict:
        # traction_worker_node는 State 키(worker_results·review_requests)를 읽으므로 WorkerInput용 함수를 직접 쓴다
        return {"worker_results": {"traction": evaluate_worker(worker_input)}}
    return {"clinical": clinical_analysis_node, "market": market_agent_node,
            "traction": traction, "risk": risk_worker_node}


def _failed_result(payload: dict, worker: str, error: Exception) -> dict:
    prev = payload.get("previous_result") or {}
    return {"run_id": payload.get("run_id"), "company_id": payload.get("company_id"), "as_of": payload.get("as_of"),
            "schema_version": payload.get("schema_version"), "criteria_version": payload.get("criteria_version"),
            "agent": worker, "dataset_version": dataset_version(payload),
            "result_version": int(prev.get("result_version") or 0) + 1, "analysis_status": "failed",
            "data": {}, "sources": [], "evidence": [], "findings": [], "missing_items": [], "coverage": [],
            "review_responses": [],
            "errors": [{"code": "WORKER_EXCEPTION", "message": f"{type(error).__name__}: {error}"[:300]}]}


def _worker_node(worker: str, fn: WorkerFn):
    def node(payload: dict) -> dict:
        try:
            out = fn(payload)
            # clinical·traction·risk는 {"worker_results": {w: result}}, market은 result 자체를 반환한다
            result = out["worker_results"][worker] if isinstance(out, dict) and "worker_results" in out else out
            if not isinstance(result, dict):
                raise TypeError(f"{worker} Worker 반환값이 dict가 아님: {type(result).__name__}")
        except Exception as e:  # noqa: BLE001 - Worker 하나 실패가 심사 전체를 멈추지 않게 한다
            result = _failed_result(payload, worker, e)
        log = {"worker": worker, "round": (payload.get("review_request") or {}).get("attempt", 0),
               "records": len((payload.get("dataset") or {}).get("records") or []),
               "analysis_status": result.get("analysis_status"),
               "review_request_id": (payload.get("review_request") or {}).get("request_id")}
        return {"worker_results": {worker: result}, "worker_log": [log]}
    node.__name__ = f"{worker}_worker"
    return node


WORKER_INPUT_KEYS = ("run_id", "company_id", "as_of", "schema_version", "criteria_version", "company_profile",
                     "dataset", "previous_result", "review_request")


def _payload(state: CompanyState, worker: str, request: Optional[dict] = None) -> dict:
    """Send로 Worker에 넘기는 입력 = common_state.md 4장 WorkerInput 9개 키만.

    다른 키를 넣지 않는 이유: market WorkerInput은 extra="forbid"라 모르는 키가 있으면 검증 실패한다.
    """
    values = {k: state.get(k) for k in WORKER_INPUT_KEYS[:6]}
    values.update(dataset=slice_dataset(load_dataset(cast(dict, state)), worker, state.get("company_id")),
                  previous_result=(state.get("worker_results") or {}).get(worker), review_request=request)
    return values


# ── WorkerResult → 심사 입력(AgentResult) ─────────────────────────────────
def _unknowns(result: dict) -> list[str]:
    return [("[필수] " if m.get("required_for_assessment") else "") + f"{m.get('item')}: {m.get('reason')}"
            for m in result.get("missing_items") or []]


def _clinical_view(result: dict) -> AgentResult:
    """공통 coverage의 not_applicable(규제 비대상 제품)을 검토 완료로 본다.
    기존 clinical 어댑터는 reviewed·no_relevant_evidence_found만 인정해, 비대상 제품이 있으면 G01이 not_checked가 된다."""
    coverage = [{**c, "status": "reviewed"} if c.get("area") == "regulatory" and c.get("status") == "not_applicable"
                else c for c in result.get("coverage") or []]
    return adapt_clinical({**result, "coverage": coverage})


# risk 어댑터는 공통 Worker 형식(data.areas·sources·findings/evidence)을 직접 읽는다
ADAPTERS = {"clinical": _clinical_view, "market": adapt_market, "traction": adapt_traction, "risk": adapt_risk}


def to_agent_result(worker: str, result: Optional[dict], company_id: str) -> AgentResult:
    if not result:
        return not_run(worker, company_id)
    ar = ADAPTERS[worker](result)
    unknowns = _unknowns(result)
    return ar.model_copy(update={"unknowns": unknowns}) if unknowns else ar


def adapt_all(state: CompanyState) -> list[AgentResult]:
    results = state.get("worker_results") or {}
    return [to_agent_result(w, results.get(w), state["company_id"]) for w in WORKERS]


# ── 보완 요청 (공통 형식) ──────────────────────────────────────────────────
def build_review_request(state: CompanyState, worker: str, issues: list, round_: int) -> dict:
    """common_state.md ReviewRequest. Worker마다 질문 키가 달라(question/text) 둘 다 넣는다."""
    cid = state["company_id"]
    prev = (state.get("worker_results") or {}).get(worker) or {}
    criteria = sorted({i.criterion_id for i in issues if i.criterion_id})
    kinds = {i.kind for i in issues}
    texts = list(dict.fromkeys(_question(i) for i in issues))
    return {
        "request_id": f"{cid}:{worker}:rr{round_}", "company_id": cid, "target_agent": worker,
        "attempt": round_, "reason": next(k for k in REASON_PRIORITY if k in kinds).value,
        "criterion_id": criteria[0] if criteria else None, "blocker": any(i.blocking for i in issues),
        "questions": [{"question_id": f"q{n:02d}", "question": t, "text": t} for n, t in enumerate(texts, 1)],
        "target_finding_ids": [f["finding_id"] for f in prev.get("findings") or []
                               if set(f.get("related_criterion_ids") or []) & set(criteria)],
        "related_evidence_ids": sorted({e for i in issues for e in i.related_ids}),
        "dataset_version": dataset_version(cast(dict, state)),
        "previous_result_version": prev.get("result_version"), "as_of": state["as_of"],
    }


# ── 노드 ──────────────────────────────────────────────────────────────────
def _orchestrator_node(enabled: Iterable[str], min_records: int):
    enabled = tuple(enabled)

    def orchestrator(state: CompanyState) -> dict:
        dataset = load_dataset(cast(dict, state))
        plan = plan_workers(dataset, state.get("company_id"), enabled, min_records)
        update: dict[str, Any] = {"plan": plan, "review_round": 0, "review_requests": {},
                                  "schema_version": state.get("schema_version") or SCHEMA_VERSION,
                                  "criteria_version": state.get("criteria_version") or CRITERIA_VERSION}
        if state.get("dataset") is not None:      # 인라인 원문 → 파일 참조로 바꾸고 State에서는 비운다
            update.update(dataset_ref=put_dataset(dataset), dataset=None)
        return update
    return orchestrator


def dispatch(state: CompanyState):
    """동적 fan-out: 계획에서 고른 Worker에만 Send. 하나도 없으면 바로 finalize."""
    selected = (state.get("plan") or {}).get("selected") or []
    return [Send(w, _payload(state, w)) for w in selected] or "finalize"


def _quality_node(policy: Policy, max_rounds: int):
    def quality_check(state: CompanyState) -> dict:
        round_ = state.get("review_round", 0)
        selected = set((state.get("plan") or {}).get("selected") or [])
        results = adapt_all(state)
        issues = validate(state["company_id"], state["as_of"], results, policy)
        status = {r.agent: r.analysis_status for r in results}
        grouped: dict[str, list] = {}
        if round_ < max_rounds:
            for i in issues:
                w = i.target_agent
                # 실행 대상이 아니었거나 실패한 Worker, 보완으로 못 고치는 이슈는 요청하지 않는다
                if w is not None and w in selected and i.reviewable and status.get(w) not in ("failed", "not_run"):
                    grouped.setdefault(w, []).append(i)
        requests = {w: build_review_request(state, w, grouped[w], round_ + 1) for w in WORKERS if w in grouped}
        history = {"round": round_, "issue_ids": [i.issue_id for i in issues],
                   "requested": {w: r["request_id"] for w, r in requests.items()},
                   "stopped_by_cap": round_ >= max_rounds and bool(issues)}
        update: dict[str, Any] = {"review_requests": requests, "review_history": [history]}
        if requests:
            update["review_round"] = round_ + 1
        return update
    return quality_check


def route_after_quality(state: CompanyState):
    requests = state.get("review_requests") or {}
    return [Send(w, _payload(state, w, req)) for w, req in requests.items()] or "finalize"


def _finalize_node(policy: Policy):
    def finalize(state: CompanyState) -> dict:
        results = adapt_all(state)
        issues = validate(state["company_id"], state["as_of"], results, policy)
        review = judge(state["company_id"], state["as_of"], [c for r in results for c in r.criteria],
                       [g for r in results for g in r.gates], issues, policy)
        plan = state.get("plan") or {}
        skipped = [f"[orchestrator] {WORKER_KO[w]} 미실행: {why}" for w, why in (plan.get("skipped") or {}).items()]
        worker_results = state.get("worker_results") or {}
        history = state.get("review_history") or []
        review = review.model_copy(update={
            "review_request_ids": [rid for h in history for rid in h.get("requested", {}).values()],
            "review_response_ids": [f"{x.get('request_id')}:{x.get('question_id')}"
                                    for res in worker_results.values() for x in res.get("review_responses") or []],
            "remaining_unknowns": skipped + review.remaining_unknowns + [u for r in results for u in r.unknowns],
            "report": ReportDraft(concerns=[f"[{r.agent}] {c}" for r in results for c in r.concerns],
                                  due_diligence_questions=[q for r in results for q in r.due_diligence_questions]),
            "source_ids": sorted({s for r in results for s in r.source_ids}),
        })
        out = review.model_dump(mode="json")
        out["orchestration"] = {"plan": plan, "review_rounds": state.get("review_round", 0),
                                "review_history": history, "worker_log": state.get("worker_log") or []}
        return {"investment_review": out}
    return finalize


# ── 그래프 조립 ───────────────────────────────────────────────────────────
def build_company_graph(workers: Optional[dict[str, WorkerFn]] = None, policy: Policy = DEFAULT_POLICY,
                        max_review_rounds: int = MAX_REVIEW_ROUNDS, enabled: Iterable[str] = WORKERS,
                        min_records: int = 1, checkpointer: Any = None):
    """checkpointer: None=상위 그래프 설정을 따름(단독 실행이면 저장 안 함), False=저장 안 함, 체크포인터 객체=저장."""
    workers = workers if workers is not None else default_workers()
    g = StateGraph(CompanyState)
    g.add_node("orchestrator", _orchestrator_node(enabled, min_records))
    for w in WORKERS:
        # Worker 노드 입력은 State가 아니라 Send로 받은 WorkerInput(dict)이다
        g.add_node(w, cast(Any, _worker_node(w, workers[w])))
        g.add_edge(w, "quality_check")
    g.add_node("quality_check", _quality_node(policy, max_review_rounds))
    g.add_node("finalize", _finalize_node(policy))
    g.add_edge(START, "orchestrator")
    g.add_conditional_edges("orchestrator", dispatch, [*WORKERS, "finalize"])
    g.add_conditional_edges("quality_check", route_after_quality, [*WORKERS, "finalize"])
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def build_graph(workers: Optional[dict[str, WorkerFn]] = None, policy: Policy = DEFAULT_POLICY,
                k: Optional[int] = None, checkpointer: Any = None, **company_kwargs):
    """전체 그래프: 기업별 그래프를 병렬 실행하고 적격 기업 중 최대 K개를 선정한다.

    checkpointer를 주면 기업 단위로 진행 상황이 저장된다. 끝난 기업은 재개 시 다시 실행하지 않는다.
    (기업 그래프는 노드 안에서 호출되므로 자체 저장은 끄고(False), 기업 결과는 상위 그래프 체크포인트에 남긴다)
    """
    company_graph = build_company_graph(workers, policy, checkpointer=False, **company_kwargs)

    def fan_out(state: RunState):
        base = {k_: state.get(k_) for k_ in ("run_id", "as_of", "schema_version", "criteria_version")
                if state.get(k_)}
        return [Send("company", {**base, **c, "company_profile": c.get("company_profile") or {}})
                for c in state.get("companies") or []] or "select"

    def company(state: CompanyState) -> dict:
        out = company_graph.invoke(state)
        return {"reviews": [out["investment_review"]],
                "analyses": [{"company_id": out["company_id"], "plan": out.get("plan"),
                              "worker_results": out.get("worker_results") or {}}]}

    def selection(state: RunState) -> dict:
        reviews = state.get("reviews") or []
        picked = select([InvestmentReview.model_validate(r) for r in reviews], policy, k)
        orch = {r["company_id"]: r.get("orchestration") for r in reviews}
        return {"final_reviews": [{**r.model_dump(mode="json"), "orchestration": orch.get(r.company_id)}
                                  for r in picked]}

    g = StateGraph(RunState)
    g.add_node("company", company)
    g.add_node("select", selection)
    g.add_conditional_edges(START, fan_out, ["company", "select"])
    g.add_edge("company", "select")
    g.add_edge("select", END)
    return g.compile(checkpointer=checkpointer)
