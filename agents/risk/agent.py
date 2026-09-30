"""Bounded evidence-review loop over a frozen dataset; never searches the web."""
import re
from pathlib import Path
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from .company_analysis import analyze, passages
from .dataset import DEFAULT_MANIFEST, load_company


class RiskInput(TypedDict):
    company_profile: dict
    review_requests: NotRequired[dict]
    risk_analysis: NotRequired[dict]


class RiskOutput(TypedDict):
    risk_analysis: dict
    references: list[dict]


class RiskState(RiskInput, RiskOutput, total=False):
    dataset: dict
    dataset_sha256: str
    selected: list[dict]
    remaining: list[dict]
    review_context: dict
    history: list[dict]
    attempt: int
    route: str
    stop_reason: str
    previous: dict


def build_risk_graph(manifest_path=DEFAULT_MANIFEST, *, cache_dir=None, model=None,
                     caller=None, max_reviews=1, source_batch=8):
    """At most two model requests by default. Review/routing are local rules."""
    if max_reviews not in (0, 1) or not 1 <= source_batch <= 8:
        raise ValueError('max_reviews must be 0 or 1; source_batch must be 1..8')
    cache_dir = Path(cache_dir) if cache_dir else Path(__file__).parent / 'data/model_cache'

    def load_dataset(state):
        profile = state['company_profile']
        dataset, digest = load_company(manifest_path, profile['company_id'])
        if profile.get('company_name') not in (None, dataset['company_profile']['company_name']):
            raise ValueError('Company name does not match the frozen dataset')
        request = (state.get('review_requests') or {}).get('risk')
        context = {}
        if request:
            if request.get('company_id') != profile['company_id'] or request.get('attempt', 1) != 1:
                raise ValueError('Review company mismatch or review limit exceeded')
            questions = request.get('questions')
            if not isinstance(questions, list) or not questions:
                raise ValueError('Review requires concrete questions')
            previous = state.get('risk_analysis') or {}
            if previous.get('company_id') != profile['company_id']:
                raise ValueError('Review requires previous analysis for this company')
            context = {'questions': questions, 'reason': request.get('reason'),
                       'previous_areas': previous.get('areas', [])}
        # Filter unusable sources before selecting a bounded batch.
        unique = {}
        for source in dataset['company_evidence']:
            if passages({**dataset, 'company_evidence': [source]}):
                unique.setdefault((source['url'], source['content']), source)
        candidates = list(unique.values())
        return {'dataset': dataset, 'dataset_sha256': digest, 'selected': [],
                'remaining': candidates, 'review_context': context, 'history': [],
                'attempt': 0, 'previous': {}, 'stop_reason': ''}

    def select_evidence(state):
        text = str(state['review_context'].get('questions', ''))
        terms = set(re.findall(r'[가-힣A-Za-z]{2,}', text))
        ranked = sorted(state['remaining'], key=lambda s: (
            -sum(t in s['content'] for t in terms), s['source_id']))
        chosen = ranked[:source_batch]
        return {'selected': state['selected'] + chosen, 'remaining': ranked[source_batch:]}

    def analyze_risk(state):
        dataset = {**state['dataset'], 'company_evidence': state['selected'],
                   'risk_review_context': state['review_context']}
        result = analyze(dataset, cache_dir, model=model, caller=caller)
        result['dataset_sha256'] = state['dataset_sha256']
        return {'risk_analysis': result}

    def review_result(state):
        result = state['risk_analysis']
        questions = [{'category': a['category'], 'question': q}
                     for a in result['areas'] for q in a['due_diligence_questions']]
        unknowns = [u for a in result['areas'] for u in a['unknowns']]
        needs_review = bool(unknowns or result['diagnostics'] or questions)
        if result['analysis_status'] == 'failed':
            reason = 'analysis_failed'
        elif result['analysis_status'] == 'no_evidence':
            reason = 'no_evidence'
        elif not needs_review:
            reason = 'review_passed'
        elif state['attempt'] >= max_reviews:
            reason = 'review_limit'
        elif not state['remaining']:
            reason = 'no_new_evidence'
        else:
            reason = 'revisit'
        history = state['history'] + [{'attempt': state['attempt'], 'decision': reason,
            'questions': questions, 'unknowns': unknowns, 'diagnostics': result['diagnostics'],
            'source_ids': [s['source_id'] for s in state['selected']], 'mode': result['mode']}]
        update = {'route': 'retry' if reason == 'revisit' else 'finish',
                  'stop_reason': reason, 'history': history}
        if reason == 'revisit':
            update.update(attempt=state['attempt'] + 1, previous=result,
                review_context={'questions': questions or unknowns or result['diagnostics'],
                                'previous_areas': result['areas'],
                                'instruction': '추가 원문으로 미확인을 보완하고 비약을 수정하세요. 결론 변경을 강요하지 마세요.'})
        elif reason == 'analysis_failed' and state['previous']:
            # Preserve usable first-pass evidence when the follow-up request fails.
            update['risk_analysis'] = {**state['previous'], 'analysis_status': 'partial',
                'diagnostics': state['previous']['diagnostics'] + result['diagnostics']}
        return update

    def export_result(state):
        result = {**state['risk_analysis'], 'review_history': state['history'],
                  'stop_reason': state['stop_reason']}
        used = {p['source_id'] for p in result['passages']}
        return {'risk_analysis': result, 'references': [s for s in state['dataset']['company_evidence']
                                                       if s['source_id'] in used]}

    graph = StateGraph(RiskState, input_schema=RiskInput, output_schema=RiskOutput)
    for name, node in [('load_dataset', load_dataset), ('select_evidence', select_evidence),
                       ('analyze_risk', analyze_risk), ('review_result', review_result),
                       ('export_result', export_result)]:
        graph.add_node(name, node)
    graph.add_edge(START, 'load_dataset')
    graph.add_edge('load_dataset', 'select_evidence')
    graph.add_edge('select_evidence', 'analyze_risk')
    graph.add_edge('analyze_risk', 'review_result')
    graph.add_conditional_edges('review_result', lambda s: s['route'],
                                {'retry': 'select_evidence', 'finish': 'export_result'})
    graph.add_edge('export_result', END)
    return graph.compile()


class RiskAgent:
    """Callable node for a parent StateGraph."""
    def __init__(self, **kwargs):
        self.graph = build_risk_graph(**kwargs)

    def __call__(self, state):
        return self.graph.invoke({k: state[k] for k in RiskInput.__annotations__ if k in state})
