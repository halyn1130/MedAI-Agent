import json,tempfile,unittest
from pathlib import Path
from .company_analysis import analyze,passages,validate_response,CATS

class Tests(unittest.TestCase):
    def state(self):
        return {'as_of':'2026-09-30','company_profile':{'company_id':'c','company_name':'테스트기업'},'company_evidence':[{'source_id':'s','url':'https://example.com','title':'협약','published_at':'2026-01-01','content':'테스트기업은 병원과 협약을 발표했습니다. '+ '공동연구를 위한 협약입니다. '*10}]}
    def response(self,state):
        pid=passages(state)[0]['passage_id']
        areas=[{'category':c,'observations':[],'unknowns':['계약 조건 미확인'],'questions':['계약 조건은 무엇인가?']} for c in CATS]
        areas[0]['observations']=[{'statement':'협약 발표','passage_ids':[pid],'kind':'observation','conditional_impact':None}]
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'areas':areas})}}]}
    def test_cache_revalidation_no_call(self):
        with tempfile.TemporaryDirectory() as d:
            calls=[];st=self.state()
            def caller(p):calls.append(p);return self.response(st)
            a=analyze(st,d,caller,model='test');b=analyze(st,d,caller,model='test')
            self.assertEqual(len(calls),1);self.assertEqual(b['mode'],'cache')
            self.assertEqual(a['areas'],b['areas'])
            self.assertEqual(a['areas'][0]['observations'][0]['citations'][0]['text'],passages(st)[0]['text'])
    def test_one_bad_item_preserves_valid(self):
        st=self.state();raw=json.loads(self.response(st)['choices'][0]['message']['content'])
        bad=dict(raw['areas'][0]['observations'][0]);bad['passage_ids']=['invented'];raw['areas'][0]['observations'].append(bad)
        a,c,e=validate_response(raw,passages(st))
        self.assertEqual(len(a[CATS[0]]),1);self.assertEqual(e[0]['code'],'unknown_passage_id')
    def test_no_evidence_no_call(self):
        with tempfile.TemporaryDirectory() as d:
            st=self.state();st['company_evidence']=[]
            r=analyze(st,d,lambda _:self.fail('API called'),model='test')
            self.assertEqual(r['analysis_status'],'no_evidence')
    def test_bad_response_cached(self):
        with tempfile.TemporaryDirectory() as d:
            r=analyze(self.state(),d,lambda _: {'choices':[]},model='test')
            r2=analyze(self.state(),d,lambda _:self.fail('retry'),model='test')
            self.assertEqual(r2['analysis_status'],'failed');self.assertEqual(r2['mode'],'cache')
    def test_failed_api_cached_and_secret_removed(self):
        with tempfile.TemporaryDirectory() as d:
            def fail(_):raise RuntimeError('secret')
            analyze(self.state(),d,fail,model='test')
            r=analyze(self.state(),d,lambda _:self.fail('retry'),model='test')
            self.assertNotIn('secret',json.dumps(r));self.assertEqual(r['analysis_status'],'failed')
    def test_future_source_excluded(self):
        st=self.state();st['company_evidence'][0]['published_at']='2027-01-01'
        self.assertEqual(passages(st),[])
