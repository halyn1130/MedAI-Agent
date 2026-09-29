import copy
import unittest
from unittest.mock import patch, Mock

from agents.risk import RiskAgent, merge_references
from agents.risk.providers import ProviderError, TavilySearch
from agents.risk.schema import RiskAnalysis

PROFILE = {'company_id': 'demo', 'company_name': '가상기업'}
SOURCE = {'source_id': 'src', 'title': '가상 제휴 발표', 'url': 'https://example.com/a',
          'publisher': '가상기업', 'source_type': 'company_announcement',
          'checked_at': '2026-09-29', 'content': '가상기업은 데이터 협력을 발표했다.'}


class Search:
    def __init__(self, fail=False, results=()):
        self.calls = []
        self.fail = fail
        self.results = results

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.fail:
            raise ProviderError('failure')
        return self.results


class Analyzer:
    def __init__(self, bad=False, change=False):
        self.contexts = []
        self.bad, self.change = bad, change

    def analyze(self, prompt, context):
        self.contexts.append(context)
        cat = context['category']
        findings = copy.deepcopy(context['previous_findings'])
        if not context['review_request'] and context['sources']:
            findings = [{
                'finding_id': cat + '_1', 'category': cat, 'title': '가상 검토',
                'facts': [{'statement': '기업이 협력을 발표했다.',
                           'source_ids': ['fabricated' if self.bad else 'src']}],
                'potential_impact': '추가 확인 필요', 'evidence_status': 'partial',
                'issue_status': 'unknown',
                'mitigation': {'status': 'unknown', 'description': '미확인'},
                'unknowns': ['계약 조건'], 'due_diligence_questions': ['독점 여부는?']}]
        if self.change and findings:
            findings[0]['potential_impact'] = '기존 의존 해석을 철회하고 미확인으로 유지'
        return {'findings': findings,
            'coverage': {'category': cat, 'status': 'reviewed' if findings else 'no_relevant_evidence_found',
                         'finding_ids': [f['finding_id'] for f in findings]},
            'question_results': [{'question_id': q['question_id'], 'status': 'answered',
                'answer': '원문에 독점 근거 없음', 'source_ids': ['src']}
                for q in (context['review_request'] or {}).get('questions', [])]}


def request(**changes):
    out = {'request_id': 'r1', 'company_id': 'demo',
           'finding_ids': ['external_dependency_1'], 'categories': ['external_dependency'],
           'reason': 'interpretation_error', 'criterion_id': 'draft', 'criterion_rule': '미확인 유지',
           'decision_blocker': '독점 근거 없음', 'questions': [{'question_id': 'q1',
           'question': '독점 공급이 명시돼 있는가?', 'completion_condition': '원문 확인'}],
           'attempt': 1, 'max_attempts': 1, 'max_search_calls': 1}
    return {**out, **changes}


class RiskTests(unittest.TestCase):
    def initial(self):
        return RiskAgent(Search(), Analyzer()).run(PROFILE, sources=[SOURCE], as_of='2026-09-29').risk_analysis

    def test_initial_missing_data_is_not_safe(self):
        out = RiskAgent(Search(), Analyzer()).run(PROFILE)
        self.assertEqual(out.risk_analysis.findings, [])
        self.assertEqual(len(out.risk_analysis.coverage), 3)
        self.assertIsNone(out.review_response)

    def test_fake_citations_are_rejected(self):
        out = RiskAgent(Search(), Analyzer(bad=True)).run(PROFILE, sources=[SOURCE])
        self.assertEqual(out.risk_analysis.analysis_status, 'failed')
        self.assertEqual(out.risk_analysis.findings, [])

    def test_search_failure_not_reported_as_absence(self):
        out = RiskAgent(Search(fail=True), Analyzer()).run(PROFILE)
        self.assertEqual(out.risk_analysis.analysis_status, 'partial')
        self.assertTrue(all(c.status == 'search_failed' for c in out.risk_analysis.coverage))

    def test_hard_search_limit(self):
        search = Search()
        out = RiskAgent(search, Analyzer(), max_search_calls=1).run(PROFILE)
        self.assertEqual(len(search.calls), 1)
        self.assertEqual(sum(l.status == 'skipped' for l in out.risk_analysis.search_log), 2)
        self.assertEqual(out.risk_analysis.analysis_status, 'partial')

    def test_search_adapter_dates_and_no_secret_in_errors(self):
        response = Mock()
        response.json.return_value = {'results': [{'url': 'https://example.com/source',
            'title': 'Source', 'content': 'Evidence', 'published_date': 'Tue, 10 Jun 2025 17:00:00 GMT'}]}
        from datetime import date
        with patch('agents.risk.providers.requests.post', return_value=response) as post:
            sources = TavilySearch('test-only').search('query', company_id='demo', as_of=date(2026, 9, 29))
            self.assertEqual(str(sources[0].published_at), '2025-06-10')
            self.assertEqual(post.call_args.kwargs['timeout'], 30)
        import requests
        with patch('agents.risk.providers.requests.post', side_effect=requests.RequestException('secret=test-only')):
            with self.assertRaises(ProviderError) as caught:
                TavilySearch('test-only').search('query', company_id='demo', as_of=date.today())
            self.assertNotIn('test-only', str(caught.exception))

    def test_targeted_review_preserves_other_finding_in_same_category(self):
        prior = self.initial()
        extra = prior.findings[0].model_copy(update={'finding_id': 'external_other'})
        prior.findings.append(extra)
        prior.coverage[0].finding_ids.append(extra.finding_id)
        out = RiskAgent(Search(), Analyzer(change=True)).run(PROFILE, previous=prior, review_request=request())
        self.assertIn(extra, out.risk_analysis.findings)

    def test_future_evidence_cannot_be_cited(self):
        future = {**SOURCE, 'published_at': '2027-01-01'}
        out = RiskAgent(Search(results=[future]), Analyzer()).run(PROFILE, as_of='2026-09-29')
        self.assertEqual(out.risk_analysis.sources, [])

    def test_targeted_review_preserves_other_categories(self):
        prior = self.initial()
        analyzer = Analyzer(change=True)
        search = Search()
        out = RiskAgent(search, analyzer).run(PROFILE, previous=prior, review_request=request())
        self.assertEqual(len(analyzer.contexts), 1)
        self.assertIn('독점 공급', search.calls[0][0])
        self.assertEqual(out.review_response.outcome, 'updated')
        self.assertEqual(out.risk_analysis.findings[1:], prior.findings[1:])
        self.assertEqual(prior.findings[0].potential_impact, '추가 확인 필요')

    def test_no_change_ends_review(self):
        out = RiskAgent(Search(), Analyzer()).run(PROFILE, previous=self.initial(), review_request=request())
        self.assertEqual(out.review_response.outcome, 'no_new_evidence')

    def test_company_mismatch_and_retry_limit(self):
        agent = RiskAgent(Search(), Analyzer())
        with self.assertRaises(ValueError):
            agent.run(PROFILE, previous=self.initial(), review_request=request(company_id='other'))
        with self.assertRaises(ValueError):
            agent.run(PROFILE, previous=self.initial(), review_request=request(attempt=2, max_attempts=10), retry_count=1)

    def test_node_patch_and_replayed_request(self):
        state = {'company_profile': PROFILE, 'risk_analysis': self.initial().model_dump(mode='json'),
                 'review_requests': {'risk': request()}, 'retry_counts': {'risk': 0}}
        node = RiskAgent(Search(), Analyzer())
        delta = node(state)
        self.assertEqual(delta['retry_counts'], {'risk': 1})
        self.assertNotIn('company_profile', delta)
        state.update(delta)
        with self.assertRaises(ValueError):
            node(state)

    def test_source_dedup_and_reference_collision(self):
        out = RiskAgent(Search(results=[SOURCE]), Analyzer()).run(PROFILE, sources=[SOURCE])
        self.assertEqual(len(out.risk_analysis.sources), 1)
        with self.assertRaises(ValueError):
            merge_references([SOURCE], [{**SOURCE, 'url': 'https://example.com/other'}])

    def test_coverage_schema_rejects_missing_category(self):
        data = self.initial().model_dump(mode='json')
        data['coverage'].pop()
        with self.assertRaises(ValueError):
            RiskAnalysis.model_validate(data)

    def test_changed_non_target_id_is_not_committed(self):
        class Bad(Analyzer):
            def analyze(self, prompt, context):
                result = super().analyze(prompt, context)
                result['findings'][0]['finding_id'] = 'another'
                result['coverage']['finding_ids'] = ['another']
                return result
        prior = self.initial()
        out = RiskAgent(Search(), Bad()).run(PROFILE, previous=prior, review_request=request())
        self.assertEqual(out.risk_analysis.findings, prior.findings)
        self.assertEqual(out.review_response.outcome, 'unresolved')


if __name__ == '__main__':
    unittest.main()
