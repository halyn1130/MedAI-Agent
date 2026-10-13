"""보고서 품질 평가(evaluator)와 작성 ⇄ 평가 Loop(report/nodes.py)."""
import unittest

from ..nodes import build_graph
from ..report import generate_report, run_report
from ..report.context_builder import build_context
from ..report.evaluator import JudgeIssue, JudgeVerdict, data_limitations, evaluate, note_key
from ..report.nodes import MAX_REPORT_ATTEMPTS
from ..report.renderer import render
from ..report.writer import CompanyText, Narrative
from .test_graph import FakeAgents, profile
from .test_report import AS_OF, META, FakeLLM, run


class FakeJudge:
    """JudgeVerdict 하나 또는 여러 개(호출 순서대로)를 돌려준다."""

    def __init__(self, verdicts):
        self.verdicts = verdicts if isinstance(verdicts, list) else [verdicts]
        self.calls = []

    def with_structured_output(self, _schema):
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.verdicts[min(len(self.calls), len(self.verdicts)) - 1]


PASS = JudgeVerdict(groundedness=5, neutrality=5, bias=4)


class Base(unittest.TestCase):
    def setUp(self):
        self.args = run()
        self.args = (*self.args, META)
        self.ctx = build_context(*self.args)
        self.ctx["limitations"] = data_limitations(self.ctx)
        self.cid = self.ctx["selected"][0]["company_id"]
        self.name = self.ctx["selected"][0]["name"]
        names = ", ".join(c["name"] for c in self.ctx["selected"])
        self.summary = f"임상·시장·실적·운영 4개 분야를 보고 {names}를 선정했다."

    def narrative(self, select="총점 순위 1위다.", points="공개자료로 확인하지 못한 항목이 있어 실사가 필요하다."):
        return Narrative(summary=self.summary, criteria_guide="기준 안내다.",
                         companies=[CompanyText(name=self.name, selection_reason=select, points=points)])

    def evaluate(self, narrative: dict, judge=None, notes=()):
        return evaluate(render(self.ctx, narrative), self.ctx, narrative, list(notes), judge)


class RuleTests(Base):
    def test_clean_narrative_passes(self):
        v = self.evaluate({"summary": self.summary, f"points:{self.cid}": "임상 연구는 실사로 확인이 필요하다."})
        self.assertTrue(v["passed"], v["criteria"])
        self.assertEqual(v["feedback"], [])

    def test_subjective_words_fail_neutrality(self):
        v = self.evaluate({"summary": self.summary, f"select:{self.cid}": "혁신적인 기술로 시장을 선도한다."})
        self.assertFalse(v["criteria"]["neutrality"]["passed"])
        self.assertTrue(v["feedback"][0].startswith(f"select:{self.cid}: [중립성]"))

    def test_rank_by_score_is_neutral(self):
        v = self.evaluate({"summary": self.summary, f"select:{self.cid}": "총점 67점으로 1위다. 임상 근거 4점이다."})
        self.assertTrue(v["criteria"]["neutrality"]["passed"])

    def test_points_without_caveat_fail_bias(self):
        v = self.evaluate({f"points:{self.cid}": "매출이 늘고 계약이 많다."})
        self.assertFalse(v["criteria"]["bias"]["passed"])
        self.assertTrue(v["criteria"]["bias"]["issues"][0]["fixable"])

    def test_summary_must_cover_four_views(self):
        v = self.evaluate({"summary": "시장만 봤다."})
        self.assertFalse(v["criteria"]["coverage"]["passed"])
        self.assertIn("임상", v["feedback"][0])

    def test_writer_number_rejection_is_groundedness(self):
        v = self.evaluate({}, notes=[f"points:{self.cid}: 데이터에 없는 숫자 ['999'] → 템플릿 사용"])
        self.assertFalse(v["criteria"]["groundedness"]["passed"])
        self.assertEqual(note_key(v["feedback"][0]), f"points:{self.cid}")

    def test_data_limitations_are_disclosed_not_failed(self):
        self.assertTrue(self.ctx["limitations"])
        md = render(self.ctx, {})
        for lim in self.ctx["limitations"]:
            self.assertIn(lim["text"], md.split("## 4. 보완 이력·한계")[1].split("## 5.")[0])
        v = self.evaluate({})
        self.assertTrue(v["passed"])
        self.assertEqual(len(v["warnings"]), len(self.ctx["limitations"]))

    def test_undisclosed_limitation_is_unfixable(self):
        narrative = {}
        md = render({**self.ctx, "limitations": []}, narrative)
        v = evaluate(md, self.ctx, narrative, [])
        self.assertFalse(v["criteria"]["groundedness"]["passed"])
        self.assertEqual(v["feedback"], [])                       # 다시 써도 고칠 수 없음


class JudgeTests(Base):
    def test_low_score_on_narrative_is_fixable(self):
        judge = FakeJudge(JudgeVerdict(groundedness=2, neutrality=5, bias=5, issues=[
            JudgeIssue(criterion="groundedness", target=f"select:{self.cid}", problem="입력에 없는 고객 언급"),
            JudgeIssue(criterion="neutrality", target="summary", problem="통과 기준의 사소한 지적")]))
        v = self.evaluate({"summary": self.summary, f"select:{self.cid}": "대형 병원 10곳과 계약했다."}, judge)
        self.assertEqual(v["criteria"]["groundedness"]["judge_score"], 2)
        self.assertEqual(v["feedback"], [f"select:{self.cid}: [근거성] 입력에 없는 고객 언급"])
        self.assertTrue(v["criteria"]["neutrality"]["passed"])    # 4점 이상 기준의 지적은 무시

    def test_low_score_on_code_section_is_unfixable(self):
        judge = FakeJudge(JudgeVerdict(groundedness=5, neutrality=5, bias=2, issues=[
            JudgeIssue(criterion="bias", target="report", problem="회사 발표에 편중")]))
        v = self.evaluate({"summary": self.summary}, judge)
        self.assertFalse(v["passed"])
        self.assertEqual(v["feedback"], [])

    def test_judge_failure_falls_back_to_rules(self):
        class Broken(FakeJudge):
            def invoke(self, messages):
                raise RuntimeError("judge down")
        v = self.evaluate({"summary": self.summary}, Broken([]))
        self.assertTrue(v["passed"])
        self.assertTrue(v["judge"].startswith("failed"))

    def test_judge_input_has_facts_and_narrative(self):
        judge = FakeJudge(PASS)
        self.evaluate({"summary": self.summary}, judge)
        self.assertIn(self.summary, judge.calls[0][1][1])
        self.assertIn("selected_list", judge.calls[0][1][1])


class LoopTests(Base):
    def test_retry_then_pass(self):
        llm = FakeLLM([self.narrative(select="혁신적인 기업이다."), self.narrative()])
        out = run_report(*self.args, llm=llm, fetch=None)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("[중립성]", llm.calls[1][-1][1])                # 반려 사유를 작성 노드에 전달
        self.assertEqual(out["report_status"], "passed")
        self.assertEqual([h["status"] for h in out["eval_history"]], ["retry", "passed"])
        self.assertIn("총점 순위 1위다.", out["report_markdown"])

    def test_only_rejected_item_is_replaced(self):
        first = self.narrative(select="혁신적인 기업이다.", points="첫 논점이다. 실사로 확인이 필요하다.")
        second = self.narrative(points="두 번째 논점이다. 실사로 확인이 필요하다.")
        out = run_report(*self.args, llm=FakeLLM([first, second]), fetch=None)
        self.assertIn("첫 논점이다.", out["report_markdown"])         # 통과한 항목은 유지
        self.assertIn("총점 순위 1위다.", out["report_markdown"])

    def test_stops_at_max_attempts(self):
        llm = FakeLLM(self.narrative(select="유망한 기업이다."))
        out = run_report(*self.args, llm=llm, fetch=None)
        self.assertEqual(len(llm.calls), MAX_REPORT_ATTEMPTS)
        self.assertEqual(out["report_status"], "max_attempts")

    def test_unfixable_does_not_loop(self):
        llm = FakeLLM(self.narrative())
        judge = FakeJudge(JudgeVerdict(groundedness=5, neutrality=5, bias=2, issues=[
            JudgeIssue(criterion="bias", target="report", problem="출처 편중")]))
        out = run_report(*self.args, llm=llm, fetch=None, judge=judge)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(out["report_status"], "unfixable")

    def test_judge_feedback_triggers_rewrite(self):
        judge = FakeJudge([JudgeVerdict(groundedness=5, neutrality=2, bias=5, issues=[
            JudgeIssue(criterion="neutrality", target=f"select:{self.cid}", problem="우열 비교 표현 삭제")]), PASS])
        llm = FakeLLM(self.narrative())
        out = run_report(*self.args, llm=llm, fetch=None, judge=judge)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(out["report_status"], "passed")
        self.assertEqual(out["eval_history"][1]["judge_scores"], {"groundedness": 5, "neutrality": 5, "bias": 4})

    def test_template_only_never_loops(self):
        md, check = generate_report(*self.args, fetch=None)
        self.assertEqual(check["evaluation"]["attempts"], 1)
        self.assertIn(check["evaluation"]["status"], ("passed", "unfixable"))
        for key in ("pages", "issues", "ok", "narrative", "references"):   # 기존 report_check 키 유지
            self.assertIn(key, check)


class FullGraphTests(unittest.TestCase):
    def test_report_nodes_after_select(self):
        companies = [profile(f"c{i:03d}") for i in range(1, 4)]
        graph = build_graph(FakeAgents().as_dict(), report={"llm": None, "judge": None, "fetch": None})
        out = graph.invoke({"run_id": "t", "trace_id": "t-1", "as_of": AS_OF, "meta": META, "companies": companies})
        self.assertEqual(len(out["final_reviews"]), 3)
        self.assertIn("## REFERENCE", out["report_markdown"])
        self.assertIn(out["report_status"], ("passed", "unfixable"))
        nodes = set(graph.get_graph().nodes)
        self.assertTrue({"aggregate", "write_report", "evaluate_report"} <= nodes)

    def test_without_report(self):
        graph = build_graph(FakeAgents().as_dict())
        self.assertNotIn("aggregate", set(graph.get_graph().nodes))


if __name__ == "__main__":
    unittest.main()
