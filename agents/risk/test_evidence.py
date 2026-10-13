import unittest
from .evidence import passages, validate_response, CATS

class EvidenceTests(unittest.TestCase):
    def state(self):
        return {'as_of':'2026-09-30','company_profile':{'company_name':'테스트기업'},
                'company_evidence':[{'source_id':'s','url':None,'title':'협약','published_at':'2026-01-01',
                                     'content':'테스트기업은 병원과 공동연구 협약을 발표했습니다. '*10}]}

    def test_bad_citation_does_not_erase_valid_observation(self):
        chunks=passages(self.state())
        good={'statement':'병원과 공동연구 협약 발표','passage_ids':[chunks[0]['passage_id']],
              'kind':'observation','conditional_impact':None}
        raw={'areas':[{'category':c,'observations':[],'unknowns':[],'questions':[]} for c in CATS]}
        raw['areas'][0]['observations']=[good,{**good,'passage_ids':['invented']}]
        accepted,_,errors=validate_response(raw,chunks)
        self.assertEqual(len(accepted[CATS[0]]),1)
        self.assertEqual(errors[0]['code'],'unknown_passage_id')

    def test_future_source_excluded(self):
        state=self.state();state['company_evidence'][0]['published_at']='2027-01-01'
        self.assertEqual(passages(state),[])
