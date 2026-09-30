import json
import re
import unittest

from ..report import generate_report
from ..report.checker import check_narrative, check_report
from ..report.context_builder import build_context
from ..report.renderer import render
from ..report.writer import Narrative, CompanyPoint
from .test_graph import FakeAgents, profile
from ..nodes import build_graph

AS_OF = "2026-09-30"
META = {"as_of": AS_OF, "criteria_version": "proposed-2.0", "policy": "zero_fill", "k": 5}
SECTIONS = ["## 1. SUMMARY", "## 2. 선정 기업", "## 3. 전체 후보 판정", "## 4. 보완 이력·한계",
            "## 5. 추가 실사 질문", "## REFERENCE"]


def run(n=3):
    companies = [profile(f"c{i:03d}") for i in range(1, n + 1)]
    companies[-1]["risk_company_id"] = None  # 판단불가 1개
    graph = build_graph(FakeAgents().as_dict())
    out = graph.invoke({"run_id": "t", "as_of": AS_OF, "companies": companies})
    # 전체 그래프 결과(analyses) → {company_id: {...}}
    analyses = {a["company_id"]: a for a in out["analyses"]}
    names = {c["company_id"]: {**c, "company_name": f"기업{c['company_id'][-1]}", "대분류": "의료 AI"} for c in companies}
    return out["final_reviews"], analyses, names


class FakeLLM:
    def __init__(self, narrative=None, fail=False):
        self.narrative, self.fail = narrative, fail

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        if self.fail:
            raise RuntimeError("llm down")
        return self.narrative


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.reviews, self.analyses, self.companies = run()
        self.ctx = build_context(self.reviews, self.analyses, self.companies, META)

    def test_sections_in_order_and_reference_last(self):
        md = render(self.ctx)
        positions = [md.index(s) for s in SECTIONS]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("\n## ", md[md.index("## REFERENCE") + 3:])

    def test_template_report_passes_checks(self):
        md = render(self.ctx)
        check = check_report(md, self.ctx)
        self.assertTrue(check["ok"], check["issues"])
        self.assertLessEqual(check["pages"], 5)

    def test_every_candidate_listed_and_undetermined_reason(self):
        md = render(self.ctx)
        for name in ("기업1", "기업2", "기업3"):
            self.assertIn(f"| {name} |", md)
        self.assertIn("판단불가", md)

    def test_references_are_cited_and_real(self):
        md = render(self.ctx)
        body = md.split("## REFERENCE")[0]
        cited = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
        self.assertEqual(cited, set(range(1, len(self.ctx["references"]) + 1)))
        self.assertTrue(all(r.get("url") for r in self.ctx["references"]))

    def test_no_selected(self):
        ctx = {**self.ctx, "selected": [], "references": []}
        md = render(ctx)
        self.assertIn("적격 기업이 없어 선정하지 않았다", md)

    def test_checker_flags_bad_citation_and_missing_company(self):
        md = render(self.ctx).replace("| 기업3 |", "| 누락 |") + "\n본문 [999]"
        issues = " ".join(check_report(md.replace("## REFERENCE", "본문 [999]\n## REFERENCE"), self.ctx)["issues"])
        self.assertIn("판정 표에 없는 기업", issues)
        self.assertIn("REFERENCE에 없는 인용 번호 [999]", issues)


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        reviews, analyses, companies = run()
        self.args = (reviews, analyses, companies, META)
        self.ctx = build_context(*self.args)
        self.cid = self.ctx["selected"][0]["company_id"]

    def test_unknown_number_is_dropped(self):
        total = self.ctx["selected"][0]["total"]
        clean, issues = check_narrative({"summary": f"후보 2개 중 {total:g}점 기업 선정", self.cid: "매출 999억 원",
                                         "c999": "없는 기업"}, self.ctx)
        self.assertEqual(sorted(clean), ["summary"])
        self.assertEqual(len(issues), 2)

    def test_generate_report_with_llm(self):
        narrative = Narrative(summary="LLM 요약 문장이다.", points=[CompanyPoint(company_id=self.cid, point="LLM 논점이다.")])
        md, check = generate_report(*self.args, llm=FakeLLM(narrative), fetch=None)
        self.assertIn("LLM 요약 문장이다.", md)
        self.assertIn("**핵심 검토 논점**: LLM 논점이다.", md)
        self.assertEqual(check["narrative"]["used"], sorted([self.cid, "summary"]))

    def test_llm_failure_falls_back_to_template(self):
        md, check = generate_report(*self.args, llm=FakeLLM(fail=True), fetch=None)
        self.assertIn("- **분석 개요**: 기준일 2026-09-30", md)
        self.assertIn("- **선정 기업**", md)
        self.assertIn("템플릿 문장", check["narrative"]["notes"][0])

    def test_default_is_template(self):
        md, check = generate_report(*self.args, fetch=None)
        self.assertEqual(check["narrative"], {"used": [], "notes": []})


if __name__ == "__main__":
    unittest.main()


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            for kwargs in ({"channel": "chrome"}, {}):
                try:
                    p.chromium.launch(**kwargs).close()
                    return True
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return False


class PdfTests(unittest.TestCase):
    def setUp(self):
        reviews, analyses, companies = run()
        self.md = render(build_context(reviews, analyses, companies, META))

    def test_html_has_tables_headings_and_reference_class(self):
        from ..report.pdf import to_html
        page = to_html(self.md)
        self.assertIn("<table>", page)
        self.assertIn("<h4>고객 문제·시장</h4>", page)
        self.assertIn('<ol class="reference">', page)
        self.assertIn('lang="ko"', page)

    @unittest.skipUnless(_browser_available(), "PDF용 브라우저(Chrome/Chromium) 없음")
    def test_pdf_written_and_page_count(self):
        import tempfile
        from pathlib import Path
        from ..report.pdf import to_pdf
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.pdf"
            pages = to_pdf(self.md, path)
            self.assertTrue(path.read_bytes().startswith(b"%PDF"))
            self.assertTrue(1 <= pages <= 5)


class CitationTests(unittest.TestCase):
    def test_kind(self):
        from ..report.citations import kind
        self.assertEqual(kind({"url": "https://pubmed.ncbi.nlm.nih.gov/123/"}), "paper")
        self.assertEqual(kind({"url": "https://dart.fss.or.kr/x", "title": "A사 감사보고서"}), "report")
        self.assertEqual(kind({"url": "https://www.khidi.or.kr/board", "title": "연구"}), "report")
        self.assertEqual(kind({"url": "https://www.etnews.com/1", "source_type": "news"}), "web")

    def test_paper_format(self):
        from ..report.citations import format_reference
        meta = {"authors": ["Lee SH", "Kim J", "Park H", "Choi Y"], "year": "2026", "journal": "J Am Heart Assoc",
                "volume": "15", "issue": "17", "pages": "e049331", "title": "AI FFR Study."}
        ref = {"url": "https://pubmed.ncbi.nlm.nih.gov/1/", "title": "AI FFR Study.", "publisher": "J Am Heart Assoc"}
        self.assertEqual(format_reference(ref, meta),
                         "Lee SH, Kim J, Park H et al.(2026). AI FFR Study. *J Am Heart Assoc*, 15(17), e049331.")
        # 서지 정보 조회 실패: 없는 값은 지어내지 않는다
        self.assertEqual(format_reference({**ref, "published_at": "2026-09-01"}),
                         "저자 미상(2026). AI FFR Study. *J Am Heart Assoc*.")

    def test_report_and_web_format(self):
        from ..report.citations import format_reference
        self.assertEqual(format_reference({"url": "https://dart.fss.or.kr/a", "title": "A사 감사보고서 (2024.12)",
                                           "publisher": "금융감독원 전자공시(DART)", "published_at": "2025-03-25"}),
                         "금융감독원 전자공시(DART)(2025). *A사 감사보고서 (2024.12)*. https://dart.fss.or.kr/a")
        self.assertEqual(format_reference({"url": "https://www.etnews.com/1", "publisher": "www.etnews.com",
                                           "title": "기사 제목 < 기업 < 기사본문 - 전자신문", "published_at": "2023-12-10"}),
                         "전자신문(2023-12-10). *기사 제목*. 전자신문, https://www.etnews.com/1")
        self.assertEqual(format_reference({"url": "https://a.com/x", "title": "T"}), "a.com(날짜 미상). *T*. a.com, https://a.com/x")

    def test_pubmed_cache(self):
        import tempfile
        from pathlib import Path
        from ..report.citations import pubmed_metadata
        refs = [{"url": "https://pubmed.ncbi.nlm.nih.gov/11/"}, {"url": "https://pubmed.ncbi.nlm.nih.gov/22/"}]
        calls = []
        fetch = lambda ids: calls.append(ids) or {i: {"year": "2025"} for i in ids}
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "c.json"
            self.assertEqual(sorted(pubmed_metadata(refs, cache, fetch)), ["11", "22"])
            pubmed_metadata(refs, cache, fetch)          # 두 번째는 캐시 사용
        self.assertEqual(calls, [["11", "22"]])


class ExplainTests(unittest.TestCase):
    def review(self, *scores, gates=None, zero=False):
        from ..contract import CriterionResult, GateResult
        from ..judge import judge
        from ..policy import CRITERIA, ZERO_FILL_POLICY
        items = [CriterionResult(criterion_id=c, score=s, score_status="scored" if s is not None else "unknown")
                 for c, s in zip(CRITERIA, scores)]
        gates = gates or [GateResult(code="G01", status="clear"), GateResult(code="G02", status="clear")]
        return judge("c1", AS_OF, items, gates, policy=ZERO_FILL_POLICY)

    def test_messages(self):
        from ..contract import GateResult
        from ..report.context_builder import explain
        self.assertEqual(explain(self.review(2, 2, 2, 2, 2, 2)), "총점 40점으로 적격 기준 60점 미만")
        self.assertEqual(explain(self.review(1, 5, 5, 5, 5, 5)), "임상 근거 1점으로 항목별 최소 기준 2점 미만")
        blocked = self.review(4, 4, 4, 4, 4, 4, gates=[GateResult(code="G01", status="confirmed", reason="판매 중지"),
                                                        GateResult(code="G02", status="clear")])
        self.assertEqual(explain(blocked), "핵심 제품의 목표국 운영을 막는 공식 조치가 확인되어 총점과 무관하게 부적격(판매 중지)")
        missing = self.review(4, 4, 4, 4, 4, 4, gates=[GateResult(code="G01", status="clear"),
                                                        GateResult(code="G02", status="not_checked", reason="05 Risk 결과 없음")])
        self.assertEqual(explain(missing), "핵심 사업 운영의 현재 중단 여부를 조사하지 못함(05 Risk 결과 없음)")


class ScoreReasonTests(unittest.TestCase):
    def test_reasons_use_names_and_real_values(self):
        reviews, analyses, companies = run()
        ctx = build_context(reviews, analyses, companies, META)
        reasons = {r["name"]: r for r in ctx["selected"][0]["score_reasons"]}
        self.assertEqual(list(reasons), ["임상 근거", "시장 성장", "고객 수요·도입", "수익화", "실적·성장", "운영 대비"])
        self.assertIn("전향적 다기관 연구", reasons["임상 근거"]["reason"])      # 가짜 보완으로 채운 C1 4점 = L3
        self.assertIn("수익화 체크 5개 중 충족", reasons["수익화"]["reason"])
        self.assertIn("매출", reasons["실적·성장"]["reason"])
        self.assertIn("위험 신호", reasons["운영 대비"]["reason"])
        md = render(ctx)
        self.assertIsNone(re.search(r"\bC[1-6]\b|\bG0[12]\b|\bRF[1-4]\b|SCORE_BELOW|MISSING_EVIDENCE", md.split("## REFERENCE")[0]))
