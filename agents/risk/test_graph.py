import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from .agent import RiskAgent, build_risk_graph
from . import test_company_analysis as fixtures


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = fixtures.Tests().state()
        self.write_dataset()

    def write_dataset(self):
        raw = json.dumps(self.state).encode()
        (self.root / 'state.json').write_bytes(raw)
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'companies': {'c': {
            'path': 'state.json', 'sha256': hashlib.sha256(raw).hexdigest()}}}))

    def agent(self, caller):
        return RiskAgent(manifest_path=self.manifest, cache_dir=self.root/'cache', model='test', caller=caller)

    def test_graph_output_cache_and_input_unchanged(self):
        calls = []
        def caller(payload):
            calls.append(payload)
            return fixtures.Tests().response(self.state)
        node = self.agent(caller)
        before = (self.root/'state.json').read_bytes()
        result = node({'company_profile': {'company_id': 'c'}, 'unrelated': 'keep'})
        again = node({'company_profile': {'company_id': 'c'}})
        self.assertEqual(set(result), {'risk_analysis', 'references'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(again['risk_analysis']['mode'], 'cache')
        self.assertEqual(result['references'][0]['source_id'], 's')
        self.assertEqual((self.root/'state.json').read_bytes(), before)

    def test_changed_dataset_rejected_before_model(self):
        (self.root/'state.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'has changed'):
            self.agent(lambda _: self.fail('API called'))({'company_profile': {'company_id': 'c'}})

    def test_no_evidence_skips_model(self):
        self.state['company_evidence'] = []
        self.write_dataset()
        result = self.agent(lambda _: self.fail('API called'))({'company_profile': {'company_id': 'c'}})
        self.assertEqual(result['risk_analysis']['analysis_status'], 'no_evidence')
        self.assertEqual(result['references'], [])

    def test_review_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Review company'):
            self.agent(lambda _: self.fail('API called'))({'company_profile': {'company_id':'c'}, 'review_requests': {'risk': {'request_id':'r'}}})

    def test_failure_returns_failed_state(self):
        def fail(_): raise RuntimeError('fake failure')
        result = self.agent(fail)({'company_profile': {'company_id': 'c'}})
        self.assertEqual(result['risk_analysis']['analysis_status'], 'failed')

    def test_embedded_parent_graph_preserves_other_state(self):
        from typing import TypedDict, Annotated
        from langgraph.graph import StateGraph, START, END
        from .legacy_agent import merge_references
        class Parent(TypedDict):
            company_profile: dict
            risk_analysis: dict
            references: Annotated[list[dict], merge_references]
            market_analysis: dict
        g=StateGraph(Parent)
        g.add_node('risk', self.agent(lambda _: fixtures.Tests().response(self.state)))
        g.add_edge(START,'risk'); g.add_edge('risk',END)
        result=g.compile().invoke({'company_profile': {'company_id':'c'},'market_analysis':{'value':1},'references':[]})
        self.assertEqual(result['market_analysis'], {'value':1})
        self.assertIn('areas',result['risk_analysis'])

    def test_loop_uses_new_evidence_questions_and_stops(self):
        self.state['company_evidence'].append({**self.state['company_evidence'][0], 'source_id':'s2', 'content': self.state['company_evidence'][0]['content'] + '추가 협력 자료입니다.'})
        self.write_dataset()
        calls=[]
        def caller(payload):
            calls.append(payload)
            return fixtures.Tests().response(self.state)
        agent=RiskAgent(manifest_path=self.manifest, cache_dir=self.root/'cache', model='test', caller=caller, source_batch=1)
        result=agent({'company_profile':{'company_id':'c'}})['risk_analysis']
        self.assertEqual(len(calls),2)
        self.assertNotIn('review_context',calls[0])
        self.assertIn('review_context',calls[1])
        self.assertGreater(len(calls[1]['passages']),len(calls[0]['passages']))
        self.assertEqual(result['stop_reason'],'review_limit')
        self.assertEqual(len(result['review_history']),2)
        agent({'company_profile':{'company_id':'c'}})
        self.assertEqual(len(calls),2)

    def test_no_unused_evidence_does_not_retry(self):
        calls=[]
        def caller(payload):
            calls.append(payload)
            return fixtures.Tests().response(self.state)
        result=self.agent(caller)({'company_profile':{'company_id':'c'}})
        self.assertEqual(len(calls),1)
        self.assertEqual(result['risk_analysis']['stop_reason'],'no_new_evidence')

    def test_external_review_passes_question_to_model(self):
        calls=[]
        def caller(payload):
            calls.append(payload)
            return fixtures.Tests().response(self.state)
        result=self.agent(caller)({'company_profile':{'company_id':'c'},
            'risk_analysis':{'company_id':'c','areas':[]},
            'review_requests':{'risk':{'company_id':'c','attempt':1,'questions':['다른 협력 기관이 있는가?']}}})
        self.assertEqual(calls[0]['review_context']['questions'],['다른 협력 기관이 있는가?'])
        self.assertIn('review_history',result['risk_analysis'])
