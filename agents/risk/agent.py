"""Common-State Risk node. Collection, dataset loading and retries belong outside."""
from typing import NotRequired, TypedDict

from .evaluator import evaluate_risk


class RiskInput(TypedDict):
    run_id: str
    company_id: str
    as_of: str
    schema_version: str
    criteria_version: str
    company_profile: dict
    dataset: dict
    previous_result: NotRequired[dict | None]
    review_request: NotRequired[dict | None]
    worker_results: NotRequired[dict]
    review_requests: NotRequired[dict]


class RiskOutput(TypedDict):
    worker_results: dict


class RiskState(RiskInput, RiskOutput):
    pass


class RiskAgent:
    """Compatibility name for a single evaluation node, not an autonomous agent."""
    def __init__(self, *, caller=None, model=None):
        self.caller = caller
        self.model = model

    def __call__(self, state):
        data = {k: state[k] for k in RiskInput.__annotations__ if k in state}
        if 'previous_result' not in data:
            data['previous_result'] = (state.get('worker_results') or {}).get('risk')
        if 'review_request' not in data:
            data['review_request'] = (state.get('review_requests') or {}).get('risk')
        return {'worker_results': {'risk': evaluate_risk(data, caller=self.caller, model=self.model)}}


def risk_worker_node(state):
    return RiskAgent()(state)


def build_risk_graph(**kwargs):
    """Optional one-node graph; no loading, conditional edges, or review loop."""
    from langgraph.graph import START, END, StateGraph
    graph = StateGraph(RiskState, input_schema=RiskInput, output_schema=RiskOutput)
    graph.add_node('risk', RiskAgent(**kwargs))
    graph.add_edge(START, 'risk')
    graph.add_edge('risk', END)
    return graph.compile()
