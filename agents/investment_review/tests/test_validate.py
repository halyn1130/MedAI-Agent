import json
import unittest
from pathlib import Path

from agents.clinical_regulatory.schema import ReviewRequest as ClinicalRequest
from agents.market.schema import ReviewRequest as MarketRequest
from agents.traction_growth.schema import ReviewRequest as TractionRequest

from ..adapters import adapt_clinical, adapt_market, adapt_risk, adapt_traction, not_run
from ..contract import FinalStatus, IssueKind
from ..judge import judge
from ..review_requests import build_review_requests
from ..validate import validate

FIXTURES = Path(__file__).parent / "fixtures"
CID, AS_OF = "c001", "2026-09-30"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def results():
    risk = load("risk_c001.json")
    return [adapt_clinical(load("clinical_c001.json")), adapt_market(load("market_c001.json")["market_analysis"]),
            adapt_traction(load("traction_c001.json")), adapt_risk(risk["risk_analysis"], risk["references"])]


def codes(issues):
    return sorted(i.issue_id.split(":", 1)[1] for i in issues)


class ValidateTests(unittest.TestCase):
    def test_fixtures(self):
        issues = validate(CID, AS_OF, results())
        self.assertEqual(codes(issues), ["clinical:score_unknown:C1", "market:score_unknown:C4"])
        self.assertFalse(any(i.blocking for i in issues))

    def test_company_and_as_of_mismatch(self):
        rs = results()
        rs[2] = rs[2].model_copy(update={"company_id": "c999", "as_of": "2026-01-01"})
        issues = {i.issue_id: i for i in validate(CID, AS_OF, rs)}
        self.assertFalse(issues["c001:traction:company_mismatch"].reviewable)
        self.assertTrue(issues["c001:traction:as_of_mismatch"].blocking)

    def test_unknown_and_missing_evidence(self):
        rs = results()
        c5 = rs[2].criteria[0]
        rs[2] = rs[2].model_copy(update={"criteria": [c5.model_copy(update={"evidence_ids": ["fake"]})]})
        c6 = rs[3].criteria[0]
        rs[3] = rs[3].model_copy(update={"criteria": [c6.model_copy(update={"evidence_ids": []})]})
        issues = {i.issue_id: i for i in validate(CID, AS_OF, rs)}
        self.assertEqual(issues["c001:traction:unknown_evidence:C5"].related_ids, ["fake"])
        self.assertEqual(issues["c001:traction:unknown_evidence:C5"].kind, IssueKind.INTERPRETATION_ERROR)
        self.assertTrue(issues["c001:risk:no_evidence:C6"].blocking)

    def test_future_source(self):
        rs = results()
        rs[3] = rs[3].model_copy(update={"source_published": {"s1": "2026-10-01", "s2": None}})
        issues = {i.issue_id: i for i in validate(CID, AS_OF, rs)}
        self.assertEqual(issues["c001:risk:future_source"].related_ids, ["s1"])
        self.assertFalse(issues["c001:risk:future_source"].blocking)

    def test_blocking_issue_makes_undetermined(self):
        rs = results()
        rs[2] = rs[2].model_copy(update={"as_of": "2026-01-01"})
        criteria = [c.model_copy(update={"score": 3.0, "score_status": "scored"}) for r in rs for c in r.criteria]
        review = judge(CID, AS_OF, criteria, [g for r in rs for g in r.gates], validate(CID, AS_OF, rs))
        self.assertEqual(review.final_status, FinalStatus.UNDETERMINED)

    def test_not_run_has_no_cross_check(self):
        self.assertEqual(codes(validate(CID, AS_OF, [not_run("market", CID)])), [])


class ReviewRequestTests(unittest.TestCase):
    def build(self, rs=None, review_round=0):
        rs = rs or results()
        return build_review_requests(CID, AS_OF, validate(CID, AS_OF, rs), rs, review_round)

    def test_fixture_requests_validate_against_teammate_schemas(self):
        reqs = self.build()
        self.assertEqual(sorted(reqs), ["clinical", "market"])
        clinical = ClinicalRequest.model_validate(reqs["clinical"])
        market = MarketRequest.model_validate(reqs["market"])  # extra 필드 금지 모델
        self.assertEqual((clinical.criterion_id, clinical.blocker, clinical.previous_result_version), ("C1", False, 1))
        self.assertEqual((market.criterion_id, market.dimensions), ("C4", ["monetization"]))
        self.assertEqual(market.request_id, "c001:market:rr1")

    def test_traction_and_risk_formats(self):
        rs = results()
        c5, c6 = rs[2].criteria[0], rs[3].criteria[0]
        rs[2] = rs[2].model_copy(update={"criteria": [c5.model_copy(update={"evidence_ids": ["fake"]})]})
        rs[3] = rs[3].model_copy(update={"criteria": [c6.model_copy(update={"score": None, "score_status": "unknown"})]})
        reqs = self.build(rs)
        traction = TractionRequest.model_validate(reqs["traction"])
        self.assertEqual((traction.reason.value, traction.criterion_id), ("interpretation_error", "C5"))
        self.assertIn("fake", traction.related_evidence_ids)
        risk = reqs["risk"]  # agents/risk/agent.py load_dataset이 확인하는 키
        self.assertEqual((risk["company_id"], risk["attempt"]), (CID, 1))
        self.assertTrue(risk["questions"] and all(isinstance(q, str) for q in risk["questions"]))

    def test_market_c3_maps_two_dimensions(self):
        rs = results()
        m = rs[1]
        crit = [c.model_copy(update={"score": None, "score_status": "unknown"}) if c.criterion_id == "C3" else c
                for c in m.criteria]
        rs[1] = m.model_copy(update={"criteria": crit})
        self.assertEqual(self.build(rs)["market"]["dimensions"], ["commercialization", "demand", "monetization"])

    def test_one_round_only(self):
        self.assertEqual(self.build(review_round=1), {})

    def test_skip_unreviewable_failed_and_not_run(self):
        rs = results()
        rs[0] = rs[0].model_copy(update={"analysis_status": "failed"})
        rs[1] = not_run("market", CID)
        rs[2] = rs[2].model_copy(update={"company_id": "c999"})
        self.assertEqual(self.build(rs), {})


if __name__ == "__main__":
    unittest.main()
