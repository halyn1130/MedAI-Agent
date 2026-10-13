"""보고서 노드 3개(취합·작성·품질 평가)의 공통 State 계약, 품질 평가 기준, 임시 Orchestrator Loop."""
import copy
import unittest

from ..report import generate_report, run_report, run_saved
from ..report.context_builder import build_context
from ..report.evaluator import JudgeIssue, JudgeVerdict, data_limitations, evaluate, note_key, to_quality_issues
from ..report.nodes import (ReportConfig, aggregate_node, evaluations_from_analyses, quality_node, report_context,
                            write_node)
from ..report.renderer import render
from ..report.writer import CompanyText, Narrative
from .test_report import AS_OF, META, FakeLLM, run

AGGREGATED_KEYS = {"based_on_results", "summary", "criteria_results", "unresolved_items", "analysis_status"}
ISSUE_KEYS = {"issue_id", "kind", "target_workers", "description", "finding_ids", "missing_ids", "questions"}


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


def to_common(worker_results: dict) -> dict:
    """기존 형식 Worker 결과 → 공통 Worker 반환 형식 (common_state.md 5번)."""
    out = {}
    for w in ("clinical", "market", "traction"):
        env = copy.deepcopy(worker_results[w])
        for e in env.get("evidence") or []:
            ids = e.pop("source_ids", None) or []
            e.update(source_id=ids[0] if ids else None, record_id=None)
        env["missing_items"] = [{"missing_id": m["item_id"], "item": m["field"], "reason": m["cause"],
                                 "related_criterion_ids": [], "required_for_assessment": m.get("impact") == "high"}
                                for m in env.get("missing_items") or []]
        env.update(agent=w, findings=[], errors=[])
        out[w] = env
    risk = worker_results["risk"]
    out["risk"] = {"company_id": risk["company_id"], "as_of": risk["as_of"], "agent": "risk", "result_version": 1,
                   "analysis_status": risk["analysis_status"], "data": {"areas": risk["areas"]},
                   "sources": risk["sources"], "findings": [], "missing_items": [], "coverage": [],
                   "review_responses": [], "errors": [],
                   "evidence": [{"evidence_id": p["passage_id"], "record_id": None, "source_id": p["source_id"],
                                 "excerpt": p.get("text"), "locator": None} for p in risk.get("passages") or []]}
    return out


class Base(unittest.TestCase):
    def setUp(self):
        reviews, analyses, companies = run()
        self.args = (reviews, analyses, companies, META)
        self.evaluations = evaluations_from_analyses(analyses, companies, reviews, "t", AS_OF)
        self.state = {"run_id": "t", "as_of": AS_OF, "criteria_version": META["criteria_version"],
                      "evaluations": self.evaluations}
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


# ── 공통 State 계약 ────────────────────────────────────────────────────────
class NodeContractTests(Base):
    def test_each_node_writes_only_its_own_keys(self):
        cfg = ReportConfig(fetch=None)
        state = dict(self.state)
        agg = aggregate_node(cfg)(state)
        self.assertEqual(set(agg), {"aggregated_result"})
        self.assertEqual(set(agg["aggregated_result"]), AGGREGATED_KEYS)
        state.update(agg)
        written = write_node(cfg)(state)
        self.assertEqual(set(written), {"report", "report_draft"})
        self.assertEqual(set(written["report"]), {"based_on_results", "content"})
        state.update(written)
        reviewed = quality_node(cfg)(state)
        self.assertEqual(set(reviewed), {"quality_review", "quality_detail"})
        self.assertEqual(set(reviewed["quality_review"]), {"based_on_results", "status", "issues"})
        self.assertIn(reviewed["quality_review"]["status"], ("pass", "needs_revision", "failed"))
        for issue in reviewed["quality_review"]["issues"]:
            self.assertEqual(set(issue), ISSUE_KEYS)
            self.assertIn(issue["kind"], ("interpretation", "missing_data", "report", "execution"))

    def test_based_on_results_records_versions(self):
        agg = aggregate_node(ReportConfig(fetch=None))(self.state)["aggregated_result"]
        self.assertEqual(agg["based_on_results"][self.cid]["clinical"], 2)     # 가짜 보완으로 result_version 2
        self.assertEqual(agg["based_on_results"][self.cid]["traction"], 1)

    def test_aggregate_matches_previous_judgement(self):
        reviews = aggregate_node(ReportConfig(fetch=None))(self.state)["aggregated_result"]["criteria_results"]["reviews"]
        before = {r["company_id"]: (r["final_status"], r["total_score"], r["selection"]["rank"]) for r in self.args[0]}
        after = {r["company_id"]: (r["final_status"], r["total_score"], r["selection"]["rank"]) for r in reviews}
        self.assertEqual(after, before)
        # 보완 요청 이력은 orchestration.retry_counts.worker_reviews에서 복원
        self.assertEqual(sorted(len(r["review_request_ids"]) for r in reviews),
                         sorted(len(r["review_request_ids"]) for r in self.args[0]))

    def test_reads_common_worker_format(self):
        common = [{**ev, "worker_results": to_common(ev["worker_results"])} for ev in self.evaluations if
                  "risk" in ev["worker_results"]]
        old = [ev for ev in self.evaluations if "risk" in ev["worker_results"]]
        cfg = ReportConfig(fetch=None)
        a = aggregate_node(cfg)({**self.state, "evaluations": old})["aggregated_result"]["criteria_results"]["reviews"]
        b = aggregate_node(cfg)({**self.state, "evaluations": common})["aggregated_result"]["criteria_results"]["reviews"]
        self.assertEqual([(r["company_id"], r["final_status"], r["total_score"]) for r in b],
                         [(r["company_id"], r["final_status"], r["total_score"]) for r in a])
        state = {**self.state, "evaluations": common}
        state.update(aggregate_node(cfg)(state))
        self.assertIn("## REFERENCE", write_node(cfg)(state)["report"]["content"])


# ── 품질 평가 기준 ─────────────────────────────────────────────────────────
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

    def test_summary_must_cover_four_views(self):
        v = self.evaluate({"summary": "시장만 봤다."})
        self.assertFalse(v["criteria"]["coverage"]["passed"])
        self.assertIn("임상", v["feedback"][0])

    def test_writer_number_rejection_is_groundedness(self):
        v = self.evaluate({}, notes=[f"points:{self.cid}: 데이터에 없는 숫자 ['999'] → 템플릿 사용"])
        self.assertFalse(v["criteria"]["groundedness"]["passed"])
        self.assertEqual(note_key(v["feedback"][0]), f"points:{self.cid}")

    def test_data_limitations_become_missing_data_issues(self):
        self.assertTrue(self.ctx["limitations"])
        md = render(self.ctx, {})
        for lim in self.ctx["limitations"]:
            self.assertIn(lim["text"], md.split("## 4. 보완 이력·한계")[1].split("## 5.")[0])   # 보고서에도 공개
        issues = to_quality_issues(self.evaluate({}), self.ctx)
        missing = [i for i in issues if i["kind"] == "missing_data"]
        self.assertEqual(len(missing), len(self.ctx["limitations"]))
        self.assertTrue(all(i["target_workers"] and i["questions"] for i in missing))

    def test_issue_kinds(self):
        v = self.evaluate({"summary": "시장만 봤다."})
        issues = to_quality_issues(v, {**self.ctx, "limitations": []})
        self.assertEqual([i["kind"] for i in issues], ["report"])
        self.assertEqual(note_key(issues[0]["description"]), "summary")
        md = render({**self.ctx, "limitations": []}, {}).replace("#### 운영 리스크", "#### 삭제")
        v = evaluate(md, {**self.ctx, "limitations": []}, {}, [])
        self.assertEqual({i["kind"] for i in to_quality_issues(v, {**self.ctx, "limitations": []})}, {"execution"})


class JudgeTests(Base):
    def test_low_score_on_narrative_is_report_issue(self):
        judge = FakeJudge(JudgeVerdict(groundedness=2, neutrality=5, bias=5, issues=[
            JudgeIssue(criterion="groundedness", target=f"select:{self.cid}", problem="입력에 없는 고객 언급"),
            JudgeIssue(criterion="neutrality", target="summary", problem="통과 기준의 사소한 지적")]))
        v = self.evaluate({"summary": self.summary, f"select:{self.cid}": "대형 병원 10곳과 계약했다."}, judge)
        self.assertEqual(v["criteria"]["groundedness"]["judge_score"], 2)
        self.assertEqual(v["feedback"], [f"select:{self.cid}: [근거성] 입력에 없는 고객 언급"])
        self.assertTrue(v["criteria"]["neutrality"]["passed"])    # 4점 이상 기준의 지적은 무시

    def test_low_score_on_code_section_is_interpretation(self):
        judge = FakeJudge(JudgeVerdict(groundedness=5, neutrality=5, bias=2, issues=[
            JudgeIssue(criterion="bias", target="report", problem="회사 발표에 편중")]))
        v = self.evaluate({"summary": self.summary}, judge)
        self.assertEqual(v["feedback"], [])
        kinds = [i["kind"] for i in to_quality_issues(v, {**self.ctx, "limitations": []})]
        self.assertEqual(kinds, ["interpretation"])

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
        self.assertIn("item_names", judge.calls[0][1][1])


# ── 임시 Orchestrator Loop (standalone.py) ─────────────────────────────────
class LoopTests(Base):
    def report_issues(self, out):
        return [i for i in out["quality_review"]["issues"] if i["kind"] == "report"]

    def test_retry_then_report_issues_resolved(self):
        llm = FakeLLM([self.narrative(select="혁신적인 기업이다."), self.narrative()])
        out, _ = run_saved(*self.args, llm=llm, fetch=None)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("[중립성]", llm.calls[1][-1][1])                # 반려 사유를 작성 노드에 전달
        self.assertEqual(self.report_issues(out), [])
        self.assertEqual(out["orchestration"]["retry_counts"]["report_revision"], 1)
        # 남은 이슈는 데이터 부족뿐 → 보고서 Loop가 아니라 Orchestrator 수집 경로 대상
        self.assertEqual(out["orchestration"]["termination_reason"], "unresolved_non_report_issues")
        self.assertIn("총점 순위 1위다.", out["report"]["content"])

    def test_only_rejected_item_is_replaced(self):
        first = self.narrative(select="혁신적인 기업이다.", points="첫 논점이다. 실사로 확인이 필요하다.")
        second = self.narrative(points="두 번째 논점이다. 실사로 확인이 필요하다.")
        out, _ = run_saved(*self.args, llm=FakeLLM([first, second]), fetch=None)
        self.assertIn("첫 논점이다.", out["report"]["content"])         # 통과한 항목은 유지
        self.assertIn("총점 순위 1위다.", out["report"]["content"])

    def test_stops_at_revision_limit(self):
        llm = FakeLLM(self.narrative(select="유망한 기업이다."))
        out, _ = run_saved(*self.args, llm=llm, fetch=None, revision_limit=2)
        self.assertEqual(len(llm.calls), 3)                            # 최초 1회 + 재작성 2회
        self.assertEqual(out["orchestration"]["termination_reason"], "report_revision_limit")

    def test_non_report_issue_does_not_rewrite(self):
        judge = FakeJudge(JudgeVerdict(groundedness=5, neutrality=5, bias=2, issues=[
            JudgeIssue(criterion="bias", target="report", problem="출처 편중")]))
        llm = FakeLLM(self.narrative())
        out, _ = run_saved(*self.args, llm=llm, fetch=None, judge=judge)
        self.assertEqual(len(llm.calls), 1)
        self.assertIn("interpretation", {i["kind"] for i in out["quality_review"]["issues"]})

    def test_template_only_never_rewrites(self):
        md, check = generate_report(*self.args, fetch=None)
        self.assertEqual(check["evaluation"]["attempts"], 1)
        for key in ("pages", "issues", "ok", "narrative", "references"):   # 기존 report_check 키 유지
            self.assertIn(key, check)

    def test_pass_when_no_issues(self):
        evaluations = [ev for ev in self.evaluations if ev["company_id"] not in {c["company_id"] for c in self.ctx["selected"]}]
        out = run_report(evaluations, "t", AS_OF, META["criteria_version"], ReportConfig(fetch=None))
        self.assertEqual(out["quality_review"]["status"], "pass")      # 선정 기업 없음 → 데이터 한계 없음
        self.assertEqual(out["orchestration"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
