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
        md, check = generate_report(*self.args, llm=FakeLLM(narrative))
        self.assertIn("LLM 요약 문장이다.", md)
        self.assertIn("**핵심 검토 논점**: LLM 논점이다.", md)
        self.assertEqual(check["narrative"]["used"], sorted([self.cid, "summary"]))

    def test_llm_failure_falls_back_to_template(self):
        md, check = generate_report(*self.args, llm=FakeLLM(fail=True))
        self.assertIn("기준일 2026-09-30 기준 Healthcare AI 후보", md)
        self.assertIn("템플릿 문장", check["narrative"]["notes"][0])

    def test_default_is_template(self):
        md, check = generate_report(*self.args)
        self.assertEqual(check["narrative"], {"used": [], "notes": []})


if __name__ == "__main__":
    unittest.main()
