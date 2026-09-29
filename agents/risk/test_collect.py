import tempfile
import unittest
from pathlib import Path
from datetime import date
import json

from .collect import collect_company, load_companies
from .providers import ProviderError
from .schema import Source


class CollectTests(unittest.TestCase):
    def test_dedup_budget_future_and_state(self):
        class Search:
            def search(self, query, **kwargs):
                base = dict(title='자료', url='https://example.com/a', publisher='기관', checked_at=date(2026, 1, 1), content='원문')
                return [Source(source_id='s1', published_at=None, **base), Source(source_id='s1', published_at=None, **base), Source(source_id='s2', published_at=date(2027, 1, 1), **base)]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'result'
            result = collect_company({'company_id': '../unsafe', 'company_name': '기업'}, Search(), folder, date(2026, 1, 1), 3)
            self.assertEqual(result['source_count'], 1)
            self.assertEqual(result['search_calls'], 3)
            self.assertEqual(list(result['coverage'].values()), [1, 1, 1])
            logs = json.loads((folder/'search_log.json').read_text())
            self.assertEqual(sum(x['status']=='budget_exhausted' for x in logs), 6)
            state = json.loads((folder/'state.json').read_text())
            self.assertEqual(state['company_evidence'][0]['source_id'], 's1')

    def test_failed_search_keeps_outputs_and_sanitizes(self):
        class Search:
            def search(self, *args, **kwargs):
                raise ProviderError('secret must not leak')
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)/'result'
            result = collect_company({'company_id':'x','company_name':'기업'}, Search(), folder, date.today(), 1)
            self.assertEqual(result['failed_calls'], 1)
            self.assertNotIn('secret', (folder/'search_log.json').read_text())
            self.assertTrue((folder/'partnerships.csv').exists())

    def test_csv_input_and_duplicate_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'companies.csv'
            path.write_text('기업명\n테스트\n', encoding='utf-8-sig')
            self.assertEqual(load_companies(path)[0]['company_name'], '테스트')
            path.write_text('기업명\n테스트\n테스트\n', encoding='utf-8-sig')
            self.assertEqual(len(load_companies(path)), 1)
            self.assertEqual(len(load_companies(path)[0]['input_records']), 2)


if __name__ == '__main__':
    unittest.main()
