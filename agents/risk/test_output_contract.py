import unittest
from unittest.mock import Mock
from .providers import ChatAnalyzer, ProviderError
from .schema import CategoryResult, Coverage
from .legacy_agent import RiskAgent

class ContractTests(unittest.TestCase):
    def test_empty_findings_with_questions_is_success(self):
        class Analyzer:
            def analyze(self, prompt, context):
                return CategoryResult(coverage=Coverage(category=context['category'], status='insufficient_information', unknowns=['미확인'], due_diligence_questions=['확인이 가능한가?']))
        search=Mock()
        result=RiskAgent(search,Analyzer(),max_search_calls=0).run({'company_id':'c','company_name':'기업'}).risk_analysis
        self.assertEqual(result.analysis_status,'complete')
        self.assertFalse(result.findings)
        self.assertTrue(all(c.unknowns for c in result.coverage))
        self.assertTrue(all(l.skip_reason=='disabled' for l in result.search_log))
        search.search.assert_not_called()

    def test_invented_quote_rejected(self):
        analyzer=ChatAnalyzer.__new__(ChatAnalyzer)
        analyzer.structured=Mock()
        analyzer.structured.invoke.return_value={'findings':[{'finding_id':'f','category':'operational_incidents','title':'사건','facts':[{'statement':'사건 발생','source_ids':['s'],'citations':[{'source_id':'s','quote':'없는 원문'}]}],'potential_impact':'영향','evidence_status':'partial','issue_status':'unknown','mitigation':{'status':'unknown','description':'미확인'}}], 'coverage':{'category':'operational_incidents','status':'reviewed','finding_ids':['f']}}
        with self.assertRaises(ProviderError) as raised:
            analyzer.analyze('',{'sources':[{'source_id':'s','content':'실제 원문'}]})
        self.assertEqual(raised.exception.diagnostic['stage'],'evidence_validation')

    def test_strict_schema_all_fields_required(self):
        from openai.lib._pydantic import to_strict_json_schema
        schema=to_strict_json_schema(CategoryResult)
        def check(node):
            if isinstance(node,dict):
                if node.get('type')=='object':
                    self.assertEqual(set(node['required']),set(node['properties']))
                    self.assertFalse(node['additionalProperties'])
                for value in node.values(): check(value)
            elif isinstance(node,list):
                for value in node: check(value)
        check(schema)

    def test_repair_once_then_empty_result(self):
        analyzer=ChatAnalyzer.__new__(ChatAnalyzer)
        analyzer.structured=Mock()
        valid={'findings':[], 'coverage':{'category':'operational_incidents','status':'insufficient_information','unknowns':['사건 여부'], 'due_diligence_questions':['미해결 사건이 있는가?']}}
        analyzer.structured.invoke.side_effect=[{'invalid':'never log this'},valid]
        result=analyzer.analyze('',{'sources':[]})
        self.assertEqual(analyzer.structured.invoke.call_count,2)
        self.assertEqual(result.findings,[])
        self.assertTrue(result.coverage.unknowns)

    def test_api_error_not_semantically_retried(self):
        analyzer=ChatAnalyzer.__new__(ChatAnalyzer)
        analyzer.structured=Mock()
        analyzer.structured.invoke.side_effect=RuntimeError('secret')
        with self.assertRaises(ProviderError) as caught:
            analyzer.analyze('',{'sources':[]})
        self.assertEqual(analyzer.structured.invoke.call_count,1)
        self.assertNotIn('secret',str(caught.exception.diagnostic))