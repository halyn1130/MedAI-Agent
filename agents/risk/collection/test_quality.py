import unittest
from .quality import screen

class QualityTests(unittest.TestCase):
    def source(self, content):
        return {'source_id':'s1','content':content,'published_at':None}

    def test_absence_is_not_safe_or_dangerous(self):
        r=screen(self.source('다른 기업의 대표 정보 '*50),'대상기업','2026-09-30')
        self.assertEqual(r['status'],'needs_review')
        self.assertIn('company_not_found_in_saved_body',r['reasons'])

    def test_broken_pdf(self):
        r=screen(self.source('%PDF-1.7 대상기업 대표 '+ 'x'*300),'대상기업','2026-09-30')
        self.assertIn('unreadable_pdf',r['reasons'])

    def test_reclassify_without_confirmation(self):
        r=screen(self.source('대상기업 대표는 병원과 협약을 발표했다. '+'연구 내용입니다. '*40),'대상기업','2026-09-30')
        self.assertEqual(r['status'],'candidate')
        self.assertIn('partnerships',r['categories'])
        self.assertIn('leadership',r['categories'])
        self.assertNotIn('operational_events',r['categories'])
        self.assertEqual(r['verification_status'],'unverified')
        self.assertEqual(r['date_status'],'unknown')

    def test_short_and_future(self):
        source=self.source('대상기업 대표')
        source['published_at']='2027-01-01'
        r=screen(source,'대상기업','2026-09-30')
        self.assertIn('insufficient_body',r['reasons'])
        self.assertIn('published_after_as_of',r['reasons'])
