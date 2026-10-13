"""전체 그래프(pipeline.py): Orchestrator 기업 그래프 → 선정 → 보고서 3노드, 품질 평가 분기 (가짜 Worker·Judge)."""
import unittest

from agents.orchestrator.test_orchestrator import AS_OF, dataset

from ..pipeline import build_pipeline, run_pipeline
from ..report.evaluator import JudgeIssue, JudgeVerdict
from ..report.nodes import ReportConfig, report_check
from ..report.writer import CompanyText, Narrative
from .test_report import FakeLLM
from .test_report_eval import FakeJudge


def _result(s: dict, agent: str, data: dict, evidence=(), findings=(), sources=()) -> dict:
    prev = s.get("previous_result") or {}
    return {"run_id": s["run_id"], "company_id": s["company_id"], "as_of": s["as_of"],
            "schema_version": s["schema_version"], "criteria_version": s["criteria_version"], "agent": agent,
            "dataset_version": s["dataset"]["version"], "result_version": int(prev.get("result_version") or 0) + 1,
            "analysis_status": "complete", "data": data, "sources": list(sources), "evidence": list(evidence),
            "findings": list(findings), "missing_items": [], "coverage": [{"area": "regulatory", "status": "reviewed"}],
            "review_responses": [{"request_id": s["review_request"]["request_id"], "question_id": q["question_id"],
                                  "answer": "재검토", "finding_ids": [], "resolved": True}
                                 for q in (s.get("review_request") or {}).get("questions", [])],
            "errors": []}


def _src(cid: str, w: str) -> list[dict]:
    return [{"source_id": f"{cid}:{w}:s{n}", "title": f"{w} 자료 {n}", "url": f"https://{w}{n}.example/{cid}",
             "publisher": f"{w}{n} 기관", "published_at": "2026-01-01"} for n in (1, 2)]


def _ev(cid: str, w: str) -> list[dict]:
    return [{"evidence_id": f"{cid}:{w}:e{n}", "source_ids": [f"{cid}:{w}:s{n}"]} for n in (1, 2)]


def _scored(cid: str, w: str, score: float = 5.0) -> dict:
    return {"score": score, "score_status": "scored", "evidence_ids": [f"{cid}:{w}:e1"]}


class EligibleFakes:
    """모든 항목을 근거와 함께 채점하는 가짜 Worker → 적격 기업. 호출(입력)을 기록한다."""

    def __init__(self, missing=()):
        self.calls = []
        self.missing = set(missing)          # {(company_id, worker)}: 데이터가 새로 오기 전까지 평가 필수 결측을 낸다

    def _gap(self, s, worker, res):
        """missing에 있는 기업·Worker는 재수집 자료(recollected 태그)가 데이터셋에 없으면 필수 결측을 남긴다."""
        recollected = any("recollected" in (r.get("tags") or []) for r in s["dataset"].get("records") or [])
        if (s["company_id"], worker) in self.missing and not recollected:
            res["missing_items"] = [{"missing_id": f"{s['company_id']}:{worker}:mi01", "item": "최근 공급 계약",
                                     "reason": "제공 자료로 확인하지 못함", "related_criterion_ids": ["C5"],
                                     "required_for_assessment": True}]
        return res

    def clinical(self, s):
        self.calls.append(("clinical", s))
        cid = s["company_id"]
        data = {"criteria_inputs": {"C1": _scored(cid, "clinical")}, "red_flags": [],
                "regulatory_records": [{"country": "KR", "authority": "식약처", "procedure_type": "허가", "status": "active",
                                        "permitted_use": "진단 보조", "evidence_ids": [f"{cid}:clinical:e1"]}]}
        return {"worker_results": {"clinical": _result(s, "clinical", data, _ev(cid, "clinical"), (), _src(cid, "clinical"))}}

    def market(self, s):
        self.calls.append(("market", s))
        cid = s["company_id"]
        data = {"criteria_inputs": {c: _scored(cid, "market") for c in ("C2", "C3", "C4")},
                "market_metrics": [{"metric_type": "cagr", "value": 0.2, "segment": "AI", "evidence_ids": [f"{cid}:market:e2"]}]}
        return _result(s, "market", data, _ev(cid, "market"), (), _src(cid, "market"))

    def traction(self, s):
        self.calls.append(("traction", s))
        cid = s["company_id"]
        data = {"criteria_inputs": {"C5": _scored(cid, "traction")}, "red_flags": [], "open_questions": ["계약 조건 확인"],
                "revenue_by_year": {"2025": {"value": 5e8, "currency": "KRW", "evidence_ids": [f"{cid}:traction:e1"]}}}
        res = _result(s, "traction", data, _ev(cid, "traction"), (), _src(cid, "traction"))
        return {"worker_results": {"traction": self._gap(s, "traction", res)}}

    def risk(self, s):
        self.calls.append(("risk", s))
        cid = s["company_id"]
        cite = [{"passage_id": "p1", "source_id": f"{cid}:risk:s1"}]
        areas = [{"category": c, "observations": [{"statement": f"{c} 관찰", "kind": "observation", "citations": cite}],
                  "unknowns": [], "due_diligence_questions": []}
                 for c in ("external_dependency", "key_person_continuity", "operational_incidents")]
        findings = [{"finding_id": f"{cid}:risk:f{n}", "category": a["category"], "claim": "관찰",
                     "evidence_ids": [f"{cid}:risk:e1"], "related_criterion_ids": ["C6"]} for n, a in enumerate(areas)]
        evidence = [{"evidence_id": f"{cid}:risk:e1", "source_id": f"{cid}:risk:s1"}]
        return {"worker_results": {"risk": _result(s, "risk", {"areas": areas}, evidence, findings, _src(cid, "risk"))}}

    def workers(self):
        return {"clinical": self.clinical, "market": self.market, "traction": self.traction, "risk": self.risk}

    def names(self):
        return [n for n, _ in self.calls]


def bundle(cid: str, name: str) -> dict:
    return {"company_id": cid, "company_profile": {"company_id": cid, "legal_name": name},
            "dataset": dataset("clinical", "market", "traction", "risk")}


def interpretation(cid: str, worker: str) -> JudgeVerdict:
    return JudgeVerdict(groundedness=2, neutrality=5, bias=5, issues=[JudgeIssue(
        criterion="groundedness", target="report", problem="매출 근거가 출처와 맞지 않음", company_id=cid, worker=worker)])


PASS = JudgeVerdict(groundedness=5, neutrality=5, bias=5)


class FakeCollector:
    """recollect.collect_additional 대역 (외부 검색 없음). status=updated면 요청한 Worker 태그로 레코드 1건을 덧붙인다."""

    def __init__(self, status="updated"):
        self.status, self.calls = status, []

    def __call__(self, company_id, profile, dataset, request, as_of):
        self.calls.append((company_id, request))
        result = {"request_id": request["request_id"], "attempt": request["attempt"], "status": self.status,
                  "based_on_dataset_version": request["based_on_dataset_version"], "dataset_version": dataset["version"],
                  "added_records": 0, "updated_workers": [], "items": []}
        if self.status != "updated":
            return {"dataset": dataset, "collection_result": result}
        workers = request["target_workers"]
        src = {"source_id": f"{company_id}:recollect:s1", "title": "기사", "url": f"https://news.example/{company_id}",
               "published_at": "2026-05-01"}
        rec = {"record_id": f"{company_id}:recollect:r1", "source_id": src["source_id"], "content": "공급 계약 체결",
               "tags": [*workers, "recollected"], "company_ids": [company_id]}
        new = {**dataset, "version": f"{dataset['version']}+r1", "sources": [*dataset["sources"], src],
               "records": [*dataset["records"], rec]}
        result.update(dataset_version=new["version"], added_records=1, updated_workers=workers)
        return {"dataset": new, "collection_result": result}


class RunMixin:
    def run_pipeline(self, judge=None, llm=None, missing=(), collector=None, **cfg):
        self.fakes = EligibleFakes(missing)
        self.collector = collector or FakeCollector("no_new_data")
        companies = [bundle("c001", "가나다"), bundle("c002", "라마바")]
        return run_pipeline(companies, "t", AS_OF, ReportConfig(fetch=None, judge=judge, llm=llm, **cfg),
                            workers=self.fakes.workers(), collector=self.collector)


class PipelineTests(RunMixin, unittest.TestCase):
    def test_graph_nodes(self):
        nodes = set(build_pipeline(EligibleFakes().workers()).get_graph().nodes)
        self.assertTrue({"company", "select", "aggregate_results", "write_report", "review_quality",
                         "orchestrator_review", "recollect_plan", "recollect_company"} <= nodes)

    def test_report_after_orchestrator(self):
        out = self.run_pipeline()
        self.assertEqual([r["final_status"] for r in out["final_reviews"]], ["eligible", "eligible"])
        self.assertIn("orchestration", out["final_reviews"][0])                 # Orchestrator 기록 유지
        self.assertIn("가나다", out["report"]["content"])
        self.assertEqual(out["quality_review"]["status"], "pass")
        self.assertEqual(out["aggregated_result"]["criteria_results"]["reviews"], out["final_reviews"])  # 재판정 없음

    def test_interpretation_goes_to_orchestrator_review(self):
        judge = FakeJudge([interpretation("c001", "traction"), PASS])
        out = self.run_pipeline(judge=judge)
        before = self.fakes.names()
        traction = [s for n, s in self.fakes.calls if n == "traction" and s["company_id"] == "c001"]
        self.assertEqual(len(traction), 2)                                      # 최초 + 재검토
        req = traction[-1]["review_request"]
        self.assertEqual(req["request_id"], "c001:traction:qr1")
        self.assertEqual(req["questions"][0]["question"], "매출 근거가 출처와 맞지 않음")
        self.assertIsNotNone(traction[-1]["previous_result"])
        self.assertEqual(before.count("clinical"), 2)                           # 다른 Worker는 재실행 안 함
        c001 = next(r for r in out["final_reviews"] if r["company_id"] == "c001")
        self.assertIn("c001:traction:qr1", c001["review_request_ids"])          # Orchestrator finalize 재판정
        self.assertEqual(out["company_states"]["c001"]["worker_results"]["traction"]["result_version"], 2)
        check = report_check(out)["evaluation"]
        self.assertEqual(check["rereviews"], 1)
        self.assertEqual(check["status"], "pass")
        self.assertEqual([h["next"] for h in check["history"]], ["orchestrator_review", "finish"])

    def test_rereview_limit(self):
        judge = FakeJudge(interpretation("c001", "traction"))                   # 계속 이상하다고 판단
        out = self.run_pipeline(judge=judge)
        self.assertEqual([n for n, s in self.fakes.calls if n == "traction" and s["company_id"] == "c001"].__len__(), 2)
        self.assertEqual(report_check(out)["evaluation"]["termination_reason"], "rereview_limit")

    def test_report_issue_rewrites_in_place(self):
        narrative = lambda select: Narrative(
            summary="임상·시장·실적·운영 4개 분야를 보고 가나다, 라마바를 선정했다.", criteria_guide="안내",
            companies=[CompanyText(name="가나다", selection_reason=select, points="실사로 확인이 필요하다.")])
        llm = FakeLLM([narrative("혁신적인 기업이다."), narrative("총점 순위 1위다.")])
        out = self.run_pipeline(llm=llm)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(self.fakes.names().count("traction"), 2)               # Worker 재실행 없음
        self.assertEqual([h["next"] for h in report_check(out)["evaluation"]["history"]], ["write_report", "finish"])


class CollectionTests(RunMixin, unittest.TestCase):
    def traction_calls(self, cid="c001"):
        return [s for n, s in self.fakes.calls if n == "traction" and s["company_id"] == cid]

    def test_data_gap_goes_to_collection_then_rerun(self):
        out = self.run_pipeline(missing={("c001", "traction")}, collector=FakeCollector("updated"))
        self.assertEqual([c for c, _ in self.collector.calls], ["c001"])          # 부족한 기업만 수집
        req = self.collector.calls[0][1]
        self.assertEqual(req["target_workers"], ["traction"])
        self.assertEqual(req["items"][0]["question"], "최근 공급 계약")
        calls = self.traction_calls()
        self.assertEqual(len(calls), 2)                                            # 최초 + 새 자료로 재평가
        self.assertTrue(any("recollected" in (r.get("tags") or []) for r in calls[-1]["dataset"]["records"]))
        self.assertIsNotNone(calls[-1]["previous_result"])
        self.assertEqual(self.fakes.names().count("clinical"), 2)                  # 다른 Worker는 재평가 안 함
        state = out["company_states"]["c001"]
        self.assertTrue(state["dataset_ref"]["version"].endswith("+r1"))           # 갱신된 데이터셋을 저장소에 다시 저장
        self.assertEqual(state["collection_history"][0]["rerun"], ["traction"])
        self.assertEqual(state["worker_results"]["traction"]["result_version"], 2)
        check = report_check(out)["evaluation"]
        self.assertEqual((check["collections"], check["collection_log"][0]["status"]), (1, "updated"))
        self.assertIn("## REFERENCE", out["report"]["content"])                    # 재판정 뒤 보고서 생성

    def test_no_new_data_skips_rerun(self):
        out = self.run_pipeline(missing={("c001", "traction")}, collector=FakeCollector("no_new_data"))
        self.assertEqual(len(self.collector.calls), 1)
        self.assertEqual(len(self.traction_calls()), 1)                            # 같은 평가를 반복하지 않음
        self.assertEqual(report_check(out)["evaluation"]["collection_log"][0]["status"], "no_new_data")
        self.assertTrue(any(u["kind"] == "missing_item" for u in out["aggregated_result"]["unresolved_items"]))

    def test_collection_limit(self):
        out = self.run_pipeline(missing={("c001", "traction")}, collector=FakeCollector("no_new_data"))
        self.assertEqual(len(self.collector.calls), 1)                             # 부족이 남아도 재수집은 1회
        self.assertEqual(report_check(out)["evaluation"]["collections"], 1)

    def test_sufficient_data_goes_straight_to_report(self):
        out = self.run_pipeline()
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(report_check(out)["evaluation"]["collections"], 0)


if __name__ == "__main__":
    unittest.main()
