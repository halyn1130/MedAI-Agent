"""Common-State boundary tests with fake model responses; never collect or call APIs."""
import copy
import json
import unittest
from unittest.mock import patch
from .agent import RiskAgent, build_risk_graph
from .evaluator import evaluate_risk
from .evidence import CATS


def fixture():
    return {'run_id': 'run1', 'company_id': 'c001', 'as_of': '2026-09-30',
            'schema_version': '2.0', 'criteria_version': 'proposed-2.0',
            'company_profile': {'legal_name': '테스트기업'},
            'dataset': {'version': 'v1', 'sources': [{'source_id': 's1', 'title': '협약',
                'url': 'https://example.com', 'published_at': '2026-01-01', 'collected_at': None}],
                'records': [{'record_id': 'r1', 'source_id': 's1', 'locator': 'page 1', 'tags': ['risk'],
                             'content': '테스트기업은 병원과 공동연구 협약을 발표했습니다. ' * 12}]}}


def reply(payload, *, review=False, bad=False):
    pid = payload['passages'][0]['passage_id']
    areas = [{'category': c, 'observations': [], 'unknowns': ['운영 체계 미확인'], 'questions': ['대행 체계가 있는가?']} for c in CATS]
    areas[0]['observations'] = [{'statement': '테스트기업의 공동연구 협약 발표',
                                'passage_ids': [pid], 'kind': 'observation', 'conditional_impact': None}]
    answers = [{'question_id': 'q1', 'answer': '공동연구 협약 발표만 확인됩니다.',
                'passage_ids': ['invented' if bad else pid], 'resolved': True}] if review else []
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'areas': areas, 'review_responses': answers})}}]}


class WorkerTests(unittest.TestCase):
    def test_one_call_no_collection_and_no_mutation(self):
        data = fixture(); before = copy.deepcopy(data); calls = []
        def caller(p): calls.append(p); return reply(p)
        with patch('agents.risk.dataset.load_company', side_effect=AssertionError('file loading')):
            result = RiskAgent(caller=caller)(data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(data, before)
        self.assertEqual(set(result), {'worker_results'})
        out = result['worker_results']['risk']
        self.assertEqual(out['analysis_status'], 'partial')
        self.assertEqual(out['dataset_version'], 'v1')
        self.assertEqual(out['evidence'][0]['record_id'], 'r1')
        self.assertIn(out['evidence'][0]['excerpt'], data['dataset']['records'][0]['content'])
        self.assertEqual(out['sources'], data['dataset']['sources'])
        self.assertEqual(out['findings'][0]['evidence_ids'], [out['evidence'][0]['evidence_id']])

    def test_review_same_data_and_version_increment(self):
        data = fixture(); previous = evaluate_risk(data, caller=reply)
        data['worker_results'] = {'risk': previous, 'market': {'keep': True}}
        data['review_requests'] = {'risk': {'request_id': 'req1', 'attempt': 5, 'reason': '해석 확인',
            'dataset_version': 'v1', 'target_finding_ids': [previous['findings'][0]['finding_id']],
            'questions': [{'question_id': 'q1', 'question': '협약의 목적은?'}]}}
        calls = []
        def caller(p): calls.append(p); return reply(p, review=True)
        out = RiskAgent(caller=caller)(data)['worker_results']['risk']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['review_context']['previous_findings'], previous['findings'])
        self.assertEqual(out['result_version'], 2)
        self.assertTrue(out['review_responses'][0]['resolved'])
        self.assertTrue(out['review_responses'][0]['finding_ids'])
        self.assertEqual(data['worker_results']['market'], {'keep': True})

    def test_invalid_review_dataset_and_company(self):
        for field, value in [('dataset_version', 'wrong'), ('company_id', 'wrong')]:
            data = fixture(); prev = evaluate_risk(data, caller=reply); prev[field] = value
            data.update(previous_result=prev, review_request={'request_id': 'req', 'attempt': 1,
                'dataset_version': 'v1', 'questions': [{'question_id': 'q1', 'question': '확인?'}]})
            out = evaluate_risk(data, caller=lambda _: self.fail('model called'))
            self.assertEqual(out['analysis_status'], 'failed')
            self.assertEqual(out['errors'][0]['code'], 'invalid_input')

    def test_unknown_review_citation_remains_unresolved(self):
        data = fixture(); data['previous_result'] = evaluate_risk(data, caller=reply)
        data['review_request'] = {'request_id': 'req', 'attempt': 1, 'dataset_version': 'v1',
            'questions': [{'question_id': 'q1', 'question': '확인?'}]}
        out = evaluate_risk(data, caller=lambda p: reply(p, review=True, bad=True))
        self.assertFalse(out['review_responses'][0]['resolved'])
        self.assertFalse(out['review_responses'][0]['finding_ids'])
        self.assertTrue(any(m['item']=='확인?' for m in out['missing_items']))

    def test_empty_or_future_evidence_no_call(self):
        for mode in ('empty', 'future'):
            data = fixture()
            if mode == 'empty': data['dataset']['records'] = []
            else: data['dataset']['sources'][0]['published_at'] = '2027-01-01'
            out = evaluate_risk(data, caller=lambda _: self.fail('model called'))
            self.assertEqual(out['analysis_status'], 'partial')
            self.assertTrue(out['missing_items'])
            self.assertFalse(out['sources'])

    def test_api_failure_never_retries_or_leaks(self):
        calls = []
        def fail(p): calls.append(p); raise RuntimeError('secret credential')
        out = evaluate_risk(fixture(), caller=fail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out['analysis_status'], 'failed')
        self.assertNotIn('secret credential', json.dumps(out))

    def test_reference_validation_before_call(self):
        data = fixture(); data['dataset']['records'][0]['source_id'] = 'unknown'
        out = evaluate_risk(data, caller=lambda _: self.fail('model called'))
        self.assertEqual(out['analysis_status'], 'failed')

    def test_same_source_multiple_records_preserve_record_identity(self):
        data = fixture(); r = copy.deepcopy(data['dataset']['records'][0]); r['record_id'] = 'r2'
        data['dataset']['records'].append(r)
        seen = []
        def caller(p): seen.extend(p['passages']); return reply(p)
        evaluate_risk(data, caller=caller)
        self.assertEqual({p['record_id'] for p in seen}, {'r1','r2'})
        self.assertEqual(len({p['passage_id'] for p in seen}), len(seen))

    def test_new_dataset_reevaluation_without_interpretation_request(self):
        data = fixture(); data['previous_result'] = evaluate_risk(data, caller=reply)
        data['dataset']['version'] = 'v2'
        out = evaluate_risk(data, caller=reply)
        self.assertEqual(out['result_version'], 2)
        self.assertEqual(out['dataset_version'], 'v2')

    def test_one_node_graph_and_parent_merge(self):
        out = build_risk_graph(caller=reply).invoke(fixture())
        self.assertIn('risk', out['worker_results'])
        from typing import Annotated, TypedDict
        from langgraph.graph import StateGraph, START, END
        class State(TypedDict):
            run_id: str
            company_id: str
            as_of: str
            schema_version: str
            criteria_version: str
            company_profile: dict
            dataset: dict
            worker_results: Annotated[dict, lambda a,b: {**a, **b}]
        g = StateGraph(State); g.add_node('risk', RiskAgent(caller=reply))
        g.add_edge(START, 'risk'); g.add_edge('risk', END)
        result = g.compile().invoke({**fixture(), 'worker_results': {'market': {'keep': True}}})
        self.assertEqual(result['worker_results']['market'], {'keep': True})
        self.assertIn('risk', result['worker_results'])


class CollectionBoundaryTests(unittest.TestCase):
    def test_explicit_frozen_loader_preserves_hash_guard(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from .collection.prepare_input import prepare_worker_input
        state = {'as_of': '2026-09-30', 'company_profile': {'company_id': 'old', 'company_name': '테스트기업'},
                 'company_evidence': [{'source_id': 's', 'title': '자료', 'url': None,
                                       'published_at': None, 'content': '원문'}]}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); raw = json.dumps(state).encode(); (p/'state.json').write_bytes(raw)
            (p/'manifest.json').write_text(json.dumps({'companies': {'old': {
                'path': 'state.json', 'sha256': hashlib.sha256(raw).hexdigest()}}}))
            data = prepare_worker_input('old', run_id='r', manifest_path=p/'manifest.json', output_company_id='c001')
            self.assertEqual(data['company_profile']['company_id'], 'c001')
            self.assertEqual(data['dataset']['records'][0]['content'], '원문')
            (p/'state.json').write_text('{}')
            with self.assertRaises(ValueError):
                prepare_worker_input('old', run_id='r', manifest_path=p/'manifest.json')
