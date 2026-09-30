"""06 LangGraph: 기업별 그래프(02~05 실행 → 검증 → 보완 1회 → 판정)와 전체 그래프(기업 병렬 → 선정).

기업 그래프
    START ─┬─ clinical ─┐
           ├─ market   ─┤
           ├─ traction ─┼→ review ─(보완 요청 있음·1회차)→ 요청받은 Agent만 재실행 → review
           └─ risk     ─┘           └────────────────────→ finalize → END

- 팀원 코드는 그대로 호출한다. 래퍼가 Agent별 입력 형식으로 State를 만들어 넘긴다.
  (review_requests: clinical·market·traction은 list, risk는 {"risk": ...}. risk는 자체 company_id 사용)
- 이미 결과가 있고 보완 요청이 없으면 Agent를 호출하지 않는다 (저장된 결과 재사용).
- Agent가 예외를 던지면 그 분석만 failed로 기록하고 계속 진행한다.
- agents 인자로 Agent를 바꿔 끼울 수 있다 (테스트·부분 실행).
"""
from __future__ import annotations

import operator
from typing import Annotated, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .adapters import adapt_clinical, adapt_market, adapt_risk, adapt_traction, not_run
from .contract import AgentResult, InvestmentReview, ReportDraft
from .judge import judge
from .policy import DEFAULT_POLICY, Policy
from .review_requests import build_review_requests
from .selection import select
from .validate import validate

AGENTS = ("clinical", "market", "traction", "risk")
AgentFn = Callable[[dict], dict]


class CompanyState(TypedDict, total=False):
    run_id: str
    as_of: str
    company_id: str
    company_profile: dict
    clinical_analysis: dict          # 이 기업의 Envelope (02 원래 State는 {company_id: Envelope})
    market_analysis: dict
    traction_analysis: dict
    risk_analysis: dict              # company_id는 06 기준 ID로 바꿔 저장, 원래 ID는 source_company_id
    references: list
    review_round: int
    review_payloads: dict            # {agent: 그 Agent 형식의 보완 요청}
    review_request_ids: list
    investment_review: dict


class RunState(TypedDict, total=False):
    run_id: str
    as_of: str
    companies: list[dict]            # 기업 프로필 목록 (company_id 필수)
    preloaded: dict                  # {company_id: {clinical_analysis: ..., market_analysis: ..., ...}}
    reviews: Annotated[list, operator.add]
    analyses: Annotated[list, operator.add]  # 기업별 최종 분석 결과 (재사용용)
    final_reviews: list


def default_agents() -> dict[str, AgentFn]:
    """팀원 Agent 노드. import가 무거워 필요할 때만 불러온다."""
    from agents.clinical_regulatory.agent import clinical_analysis_node
    from agents.market.agent import default_market_agent
    from agents.risk import RiskAgent
    from agents.traction_growth.agent import traction_growth_node
    # Risk 내부 보완은 끄고 06 보완 루프(기업당 1회)만 쓴다.
    return {"clinical": clinical_analysis_node, "market": default_market_agent(),
            "traction": traction_growth_node, "risk": RiskAgent(max_reviews=0)}


def _failed(state: CompanyState, agent: str, error: Exception) -> dict:
    return {"company_id": state["company_id"], "as_of": state["as_of"], "analysis_status": "failed",
            "agent": agent, "error": f"{type(error).__name__}: {error}"[:300]}


# ── Agent 래퍼: 06 State ↔ 각 Agent 입력·출력 형식 ─────────────────────────
def _clinical(fn: AgentFn, state: CompanyState, request: Optional[dict]) -> dict:
    cid = state["company_id"]
    prev = state.get("clinical_analysis")
    out = fn({"company_id": cid, "company_profile": state["company_profile"], "as_of": state["as_of"],
              "run_id": state["run_id"], "clinical_analysis": {cid: prev} if prev else {},
              "review_requests": [request] if request else []})
    return {"clinical_analysis": (out.get("clinical_analysis") or {}).get(cid) or prev}


def _market(fn: AgentFn, state: CompanyState, request: Optional[dict]) -> dict:
    prev = state.get("market_analysis")
    out = fn({"company_profile": state["company_profile"], "as_of": state["as_of"], "run_id": state["run_id"],
              "market_analysis": prev, "review_requests": [request] if request else []})
    return {"market_analysis": out.get("market_analysis") or prev}


def _traction(fn: AgentFn, state: CompanyState, request: Optional[dict]) -> dict:
    prev = state.get("traction_analysis")
    out = fn({"company_profile": state["company_profile"], "as_of": state["as_of"], "run_id": state["run_id"],
              "traction_analysis": prev, "review_requests": [request] if request else [],
              "clinical_analysis": state.get("clinical_analysis"), "risk_analysis": state.get("risk_analysis")})
    return {"traction_analysis": out.get("traction_analysis") or prev}


def _risk(fn: AgentFn, state: CompanyState, request: Optional[dict]) -> dict:
    cid = state["company_id"]
    rid = state["company_profile"].get("risk_company_id")
    if not rid:
        return {}  # Risk 고정 데이터에 없는 기업 → 결과 없음(not_run)
    prev = state.get("risk_analysis")
    call = {"company_profile": {"company_id": rid, "company_name": state["company_profile"].get("company_name")}}
    if prev:
        call["risk_analysis"] = {**prev, "company_id": prev.get("source_company_id", rid)}
    if request:
        call["review_requests"] = {"risk": {**request, "company_id": rid}}
    out = fn(call)
    risk = out["risk_analysis"]
    return {"risk_analysis": {**risk, "company_id": cid, "source_company_id": risk.get("company_id")},
            "references": out.get("references", [])}


WRAPPERS = {"clinical": _clinical, "market": _market, "traction": _traction, "risk": _risk}
STATE_KEY = {a: f"{a}_analysis" for a in AGENTS}


def _agent_node(agent: str, fn: AgentFn):
    def node(state: CompanyState) -> dict:
        request = (state.get("review_payloads") or {}).get(agent)
        if state.get(STATE_KEY[agent]) and not request:
            return {}  # 결과가 이미 있고 보완 요청 없음
        try:
            return WRAPPERS[agent](fn, state, request)
        except Exception as e:  # noqa: BLE001 - 한 Agent 실패가 전체 심사를 멈추지 않게 한다
            return {STATE_KEY[agent]: _failed(state, agent, e)}
    return node


# ── 06 노드 ────────────────────────────────────────────────────────────────
def adapt_all(state: CompanyState) -> list[AgentResult]:
    cid = state["company_id"]
    out = []
    for agent, adapt in (("clinical", adapt_clinical), ("market", adapt_market), ("traction", adapt_traction)):
        env = state.get(STATE_KEY[agent])
        out.append(adapt(env) if env else not_run(agent, cid))
    risk = state.get("risk_analysis")
    out.append(adapt_risk(risk, state.get("references")) if risk else not_run("risk", cid))
    return out


def _review_node(policy: Policy):
    def node(state: CompanyState) -> dict:
        results = adapt_all(state)
        issues = validate(state["company_id"], state["as_of"], results, policy)
        round_ = state.get("review_round", 0)
        payloads = build_review_requests(state["company_id"], state["as_of"], issues, results, round_)
        if not payloads:
            return {"review_payloads": {}}
        return {"review_payloads": payloads, "review_round": round_ + 1,
                "review_request_ids": [p["request_id"] for p in payloads.values()]}
    return node


def _route(state: CompanyState):
    payloads = state.get("review_payloads") or {}
    return [a for a in AGENTS if a in payloads] or "finalize"


def _finalize_node(policy: Policy):
    def node(state: CompanyState) -> dict:
        results = adapt_all(state)
        issues = validate(state["company_id"], state["as_of"], results, policy)
        review = judge(state["company_id"], state["as_of"], [c for r in results for c in r.criteria],
                       [g for r in results for g in r.gates], issues, policy)
        review = review.model_copy(update={
            "review_request_ids": state.get("review_request_ids", []),
            "review_response_ids": [x.get("response_id") or x.get("request_id") for r in AGENTS
                                    for x in (state.get(STATE_KEY[r]) or {}).get("review_responses", [])],
            "remaining_unknowns": review.remaining_unknowns + [u for r in results for u in r.unknowns],
            "report": ReportDraft(concerns=[f"[{r.agent}] {c}" for r in results for c in r.concerns],
                                  due_diligence_questions=[q for r in results for q in r.due_diligence_questions]),
            "source_ids": sorted({s for r in results for s in r.source_ids}),
        })
        return {"investment_review": review.model_dump(mode="json")}
    return node


def build_company_graph(agents: Optional[dict[str, AgentFn]] = None, policy: Policy = DEFAULT_POLICY):
    agents = agents if agents is not None else default_agents()
    g = StateGraph(CompanyState)
    for name in AGENTS:
        g.add_node(name, _agent_node(name, agents[name]))
        g.add_edge(START, name)
        g.add_edge(name, "review")
    g.add_node("review", _review_node(policy))
    g.add_node("finalize", _finalize_node(policy))
    g.add_conditional_edges("review", _route, [*AGENTS, "finalize"])
    g.add_edge("finalize", END)
    return g.compile()


def build_graph(agents: Optional[dict[str, AgentFn]] = None, policy: Policy = DEFAULT_POLICY, k: Optional[int] = None):
    """전체 그래프: 기업별 그래프를 병렬 실행하고 적격 기업 중 최대 K개를 선정한다."""
    company_graph = build_company_graph(agents, policy)

    def fan_out(state: RunState):
        pre = state.get("preloaded") or {}
        return [Send("company", {"run_id": state["run_id"], "as_of": state["as_of"],
                                 "company_id": c["company_id"], "company_profile": c,
                                 **pre.get(c["company_id"], {})})
                for c in state["companies"]] or "select"

    def company(state: CompanyState) -> dict:
        out = company_graph.invoke(state)
        kept = {k: out[k] for k in (*STATE_KEY.values(), "references") if out.get(k) is not None}
        return {"reviews": [out["investment_review"]], "analyses": [{"company_id": out["company_id"], **kept}]}

    def selection(state: RunState) -> dict:
        reviews = [InvestmentReview.model_validate(r) for r in state.get("reviews") or []]
        return {"final_reviews": [r.model_dump(mode="json") for r in select(reviews, policy, k)]}

    g = StateGraph(RunState)
    g.add_node("company", company)
    g.add_node("select", selection)
    g.add_conditional_edges(START, fan_out, ["company", "select"])
    g.add_edge("company", "select")
    g.add_edge("select", END)
    return g.compile()
