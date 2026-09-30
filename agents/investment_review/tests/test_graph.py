"""06 LangGraph를 가짜 Agent로 검증한다. 가짜 Agent는 팀원 노드와 같은 입력·출력 형식을 쓴다."""
import copy
import json
import unittest
from pathlib import Path

from agents.clinical_regulatory.schema import ReviewRequest as ClinicalRequest
from agents.market.schema import ReviewRequest as MarketRequest

from ..contract import FinalStatus, ReasonCode
from ..nodes import build_company_graph, build_graph

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = "2026-09-30"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def retag(obj, cid):
    return json.loads(json.dumps(obj).replace('"c001', f'"{cid}').replace("c001:", f"{cid}:"))


class FakeAgents:
    """샘플 결과를 돌려주고, 보완 요청을 받으면 비어 있던 점수를 채운다. 받은 입력을 기록한다."""

    def __init__(self, fail=()):
        self.calls = {a: [] for a in ("clinical", "market", "traction", "risk")}
        self.fail = set(fail)

    def _record(self, agent, state):
        self.calls[agent].append(copy.deepcopy(state))
        if agent in self.fail:
            raise RuntimeError(f"{agent} down")

    def clinical(self, state):
        self._record("clinical", state)
        cid = state["company_id"]
        env = retag(load("clinical_c001.json"), cid)
        if state["review_requests"]:
            env = {**state["clinical_analysis"][cid], "result_version": 2}
            env["evidence"] = [{"evidence_id": f"{cid}:clinical:ev99", "company_id": cid, "statement": "보완 근거",
                                "source_ids": []}]
            env["data"]["criteria_inputs"]["C1"] = {"score": 4.0, "score_status": "scored",
                                                    "evidence_ids": [f"{cid}:clinical:ev99"], "rationale": "보완"}
            env["review_responses"] = [{"response_id": "resp-clinical", "request_id": state["review_requests"][0]["request_id"]}]
        return {"clinical_analysis": {cid: env}}

    def market(self, state):
        self._record("market", state)
        cid = state["company_profile"]["company_id"]
        env = retag(load("market_c001.json")["market_analysis"], cid)
        if state["review_requests"]:
            env = copy.deepcopy(state["market_analysis"])
            for chk in env["data"]["dimensions"]["monetization"]["checks"]:
                if chk["status"] == "unknown":
                    chk["status"] = "yes"
                    chk["evidence_ids"] = env["data"]["dimensions"]["monetization"]["checks"][0]["evidence_ids"]
        return {"market_analysis": env}

    def traction(self, state):
        self._record("traction", state)
        return {"traction_analysis": retag(load("traction_c001.json"), state["company_profile"]["company_id"])}

    def risk(self, state):
        self._record("risk", state)
        out = load("risk_c001.json")
        rid = state["company_profile"]["company_id"]
        return {"risk_analysis": {**out["risk_analysis"], "company_id": rid}, "references": out["references"]}

    def as_dict(self):
        return {"clinical": self.clinical, "market": self.market, "traction": self.traction, "risk": self.risk}


def profile(cid="c001", **extra):
    return {"company_id": cid, "company_name": "레모넥스", "risk_company_id": f"company_{cid}", **extra}


def run_company(fakes, **state):
    graph = build_company_graph(fakes.as_dict())
    return graph.invoke({"run_id": "t", "as_of": AS_OF, "company_id": "c001", "company_profile": profile(), **state})


class CompanyGraphTests(unittest.TestCase):
    def test_review_loop_reruns_only_requested_agents(self):
        fakes = FakeAgents()
        out = run_company(fakes)
        # 1회차: 4개 모두, 보완: 샘플에서 비어 있는 C1(임상)·C4 미확인 체크(시장)만
        self.assertEqual({a: len(c) for a, c in fakes.calls.items()},
                         {"clinical": 2, "market": 2, "traction": 1, "risk": 1})
        ClinicalRequest.model_validate(fakes.calls["clinical"][1]["review_requests"][0])
        MarketRequest.model_validate(fakes.calls["market"][1]["review_requests"][0])
        review = out["investment_review"]
        self.assertEqual(review["review_request_ids"], ["c001:clinical:rr1", "c001:market:rr1"])
        self.assertIn("resp-clinical", review["review_response_ids"])
        # 보완 후 모든 점수 확인: C1 4, C2 4, C3 5, C4 5, C5 5, C6 4 → 16+8+15+15+25+12 = 91
        self.assertEqual((review["final_status"], review["total_score"], review["score_basis"]),
                         ("eligible", 91.0, "full"))
        self.assertEqual(out["review_round"], 1)

    def test_risk_gets_its_own_id_and_dict_request(self):
        fakes = FakeAgents()
        out = run_company(fakes)
        call = fakes.calls["risk"][0]
        self.assertEqual(call["company_profile"]["company_id"], "company_c001")
        self.assertNotIn("review_requests", call)
        self.assertEqual((out["risk_analysis"]["company_id"], out["risk_analysis"]["source_company_id"]),
                         ("c001", "company_c001"))

    def test_preloaded_results_are_not_rerun(self):
        fakes = FakeAgents()
        pre = {"traction_analysis": load("traction_c001.json"),
               "risk_analysis": {**load("risk_c001.json")["risk_analysis"], "source_company_id": "company_c001"},
               "references": load("risk_c001.json")["references"]}
        run_company(fakes, **pre)
        self.assertEqual((len(fakes.calls["traction"]), len(fakes.calls["risk"])), (0, 0))

    def test_agent_failure_is_recorded_not_raised(self):
        out = run_company(FakeAgents(fail={"traction"}))
        self.assertEqual(out["traction_analysis"]["analysis_status"], "failed")
        review = out["investment_review"]
        self.assertEqual((review["final_status"], review["reason_codes"]), ("undetermined", [ReasonCode.ANALYSIS_FAILED.value]))

    def test_company_without_risk_data(self):
        fakes = FakeAgents()
        out = run_company(fakes, company_profile=profile(risk_company_id=None))
        self.assertEqual(len(fakes.calls["risk"]), 0)
        self.assertEqual(out["investment_review"]["final_status"], FinalStatus.UNDETERMINED.value)  # G02 not_checked

    def test_report_draft_collects_questions_and_concerns(self):
        review = run_company(FakeAgents())["investment_review"]
        self.assertTrue(review["report"]["due_diligence_questions"])
        self.assertTrue(any(c.startswith("[risk]") for c in review["report"]["concerns"]))
        self.assertTrue(review["source_ids"])


class RunGraphTests(unittest.TestCase):
    def test_many_companies_and_selection(self):
        companies = [profile(f"c{i:03d}") for i in range(1, 8)]
        companies[6]["risk_company_id"] = None  # 판단불가 1개
        out = build_graph(FakeAgents().as_dict(), k=5).invoke(
            {"run_id": "t", "as_of": AS_OF, "companies": companies})
        final = out["final_reviews"]
        self.assertEqual(len(final), 7)
        self.assertEqual(sum(r["selection"]["selected"] for r in final), 5)
        self.assertEqual([r["company_id"] for r in final[:5]], ["c001", "c002", "c003", "c004", "c005"])
        self.assertEqual(final[-1]["final_status"], "undetermined")

    def test_no_companies(self):
        out = build_graph(FakeAgents().as_dict()).invoke({"run_id": "t", "as_of": AS_OF, "companies": []})
        self.assertEqual(out["final_reviews"], [])


if __name__ == "__main__":
    unittest.main()
