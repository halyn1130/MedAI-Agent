import copy
import json
import unittest
from pathlib import Path

from ..adapters import adapt_clinical, adapt_market, adapt_risk, adapt_traction, not_run
from ..contract import FinalStatus, GateStatus, IssueKind, ReasonCode, ScoreStatus
from ..judge import judge
from ..policy import PARTIAL_POLICY, STRICT_POLICY

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def by_id(result):
    return {c.criterion_id: c for c in result.criteria}


class ClinicalTests(unittest.TestCase):
    def setUp(self):
        self.env = load("clinical_c001.json")

    def test_company_level_not_applicable_passes_through(self):
        r = adapt_clinical(self.env)
        self.assertEqual((r.agent, r.company_id, r.analysis_status), ("clinical", "c001", "complete"))
        self.assertEqual(by_id(r)["C1"].score_status, ScoreStatus.NOT_APPLICABLE)
        self.assertEqual(r.gates[0].status, GateStatus.CLEAR)

    def test_scored_c1(self):
        c1 = by_id(adapt_clinical(load("clinical_c002.json")))["C1"]
        self.assertEqual((c1.score, c1.score_status), (2.0, ScoreStatus.SCORED))

    def test_cl02_maps_to_g01(self):
        for status, expected in (("confirmed", GateStatus.CONFIRMED), ("candidate", GateStatus.UNRESOLVED),
                                 ("resolved", GateStatus.CLEAR)):
            env = copy.deepcopy(self.env)
            env["data"]["red_flags"] = [{"flag_id": "f", "finding_id": "fd", "code": "CL02", "status": status,
                                         "materiality": "high", "description": "공식 판매 중지", "evidence_ids": ["e1"]}]
            self.assertEqual(adapt_clinical(env).gates[0].status, expected, status)

    def test_regulatory_not_reviewed_is_not_checked(self):
        env = copy.deepcopy(self.env)
        for c in env["coverage"]:
            if c["area"] == "regulatory":
                c["status"] = "search_failed"
        self.assertEqual(adapt_clinical(env).gates[0].status, GateStatus.NOT_CHECKED)

    def test_failed_analysis(self):
        env = copy.deepcopy(self.env)
        env["analysis_status"] = "failed"
        r = adapt_clinical(env)
        self.assertEqual(r.gates[0].status, GateStatus.NOT_CHECKED)
        self.assertEqual([(i.kind, i.blocking) for i in r.issues], [(IssueKind.ANALYSIS_FAILED, True)])


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.env = load("market_c001.json")["market_analysis"]

    def with_checks(self, **statuses):
        """영역별 체크 상태 문자열. 예: monetization="yynu?" (y=yes, n=no, u=unknown)."""
        env = copy.deepcopy(self.env)
        code = {"y": "yes", "n": "no", "u": "unknown"}
        for dim, pattern in statuses.items():
            checks = env["data"]["dimensions"][dim]["checks"]
            for chk, ch in zip(checks, pattern):
                chk["status"] = code[ch]
        return env

    def test_criteria(self):
        r = adapt_market(self.env)
        c = by_id(r)
        # C4: 동선 님 결과는 unknown(MO 1개 미확인)이지만 06은 확인된 yes 4개로 채점
        self.assertEqual((c["C2"].score, c["C3"].score, c["C4"].score), (4.0, 5.0, 4.0))
        self.assertEqual(self.env["data"]["criteria_inputs"]["C4"]["score_status"], "unknown")
        self.assertEqual(len(c["C4"].unconfirmed_checks), 1)
        self.assertTrue(any("C4 체크 미확인" in u for u in r.unknowns))
        self.assertTrue(all(x.source_agent == "market" for x in r.criteria))

    def test_confirmed_check_counting(self):
        c = by_id(adapt_market(self.with_checks(demand="yyynn", commercialization="yuuuu", monetization="nnuuu")))
        self.assertEqual(c["C3"].score, 2.0)            # (3 + 1) / 2
        self.assertEqual(len(c["C3"].unconfirmed_checks), 4)
        self.assertEqual(c["C4"].score, 0.0)            # 확인된 체크 2개가 모두 no
        c = by_id(adapt_market(self.with_checks(commercialization="uuuuu", monetization="uuuuu")))
        # 완화: 도입 영역이 전부 unknown이어도 수요(yes 5개)만으로 C3
        self.assertEqual((c["C3"].score, c["C4"].score_status), (5.0, ScoreStatus.UNKNOWN))
        self.assertIn("확인된 영역만으로 계산", c["C3"].rationale)
        c = by_id(adapt_market(self.with_checks(demand="uuuuu", commercialization="uuuuu")))
        self.assertEqual(c["C3"].score_status, ScoreStatus.UNKNOWN)

    def test_c2_cagr_fallback(self):
        env = copy.deepcopy(self.env)
        del env["data"]["criteria_inputs"]["C2"]
        c2 = by_id(adapt_market(env))["C2"]
        self.assertEqual(c2.score, 4.0)  # 샘플 CAGR 18% → 15% 이상 20% 미만
        self.assertIn("[완화]", c2.rationale)
        env["data"]["market_metrics"] = []
        env["data"]["dimensions"]["size_growth"]["market_metrics"] = []
        self.assertEqual(by_id(adapt_market(env))["C2"].score_status, ScoreStatus.UNKNOWN)

    def test_cagr_bands(self):
        from ..adapters.market import cagr_score
        for rate, score in ((-0.01, 0), (0.0, 1), (0.049, 1), (0.05, 2), (0.10, 3), (0.15, 4), (0.1999, 4), (0.20, 5)):
            self.assertEqual(cagr_score(rate), score, rate)


class TractionTests(unittest.TestCase):
    def test_c5_and_questions(self):
        env = load("traction_c001.json")
        r = adapt_traction(env)
        c5 = env["data"]["criteria_inputs"]["C5"]
        self.assertEqual(by_id(r)["C5"].score, c5["score"])
        self.assertEqual(r.due_diligence_questions, env["data"]["open_questions"])
        self.assertEqual(len(r.unknowns), len(env["missing_items"]))

    def test_red_flags_become_concerns(self):
        env = load("traction_c001.json")
        env["data"]["red_flags"] = [{"code": "RF1", "finding_id": "f", "reason": "유료 계약 미발견",
                                     "evidence_ids": [], "status": "needs_review"}]
        self.assertEqual(adapt_traction(env).concerns, ["RF1(needs_review): 유료 계약 미발견"])


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.out = load("risk_c001.json")
        self.risk = self.out["risk_analysis"]

    def with_signals(self, counts):
        """영역별 위험 신호 수를 바꾼 risk_analysis. None이면 관찰 없음."""
        risk = copy.deepcopy(self.risk)
        for area, n in zip(risk["areas"], counts):
            area["observations"] = [] if n is None else (
                [{"statement": "사실", "kind": "observation"}] +
                [{"statement": f"신호{i}", "kind": "risk_signal", "citations": []} for i in range(n)])
        return risk

    def test_fixture(self):
        r = adapt_risk(self.risk, self.out["references"])
        c6 = by_id(r)["C6"]
        # external_dependency 신호 0건=5, key_person 1건=3, operational_incidents 관찰 없음 → 제외
        self.assertEqual((c6.score, c6.score_status), (4.0, ScoreStatus.SCORED))
        self.assertIn("관찰 없음 제외: operational_incidents", c6.rationale)
        self.assertEqual(r.gates[0].status, GateStatus.CLEAR)
        self.assertEqual(len(r.due_diligence_questions), 3)
        self.assertEqual(len(r.concerns), 1)  # risk_signal만. observation은 우려로 옮기지 않음
        self.assertEqual(r.source_ids, ["s1", "s2"])
        self.assertEqual(r.issues, [])

    def test_c6_rule(self):
        for counts, expected in (([0, 0, 0], 5.0), ([1, 1, 1], 3.0), ([2, 5, 0], 7 / 3),
                                 ([None, None, 3], 1.0), ([None, None, None], None)):
            c6 = by_id(adapt_risk(self.with_signals(counts)))["C6"]
            self.assertEqual(c6.score, expected, counts)

    def test_not_run_statuses(self):
        for status, failed in (("no_evidence", False), ("failed", True)):
            r = adapt_risk({**self.with_signals([0, 0, 0]), "analysis_status": status})
            self.assertEqual(by_id(r)["C6"].score_status, ScoreStatus.UNKNOWN)
            self.assertEqual(r.gates[0].status, GateStatus.NOT_CHECKED)
            self.assertEqual(bool(r.issues), failed)


class EndToEndTests(unittest.TestCase):
    def results(self):
        risk = load("risk_c001.json")
        return [adapt_clinical(load("clinical_c001.json")),
                adapt_market(load("market_c001.json")["market_analysis"]),
                adapt_traction(load("traction_c001.json")),
                adapt_risk(risk["risk_analysis"], risk["references"])]

    def test_same_company_id(self):
        self.assertEqual({r.company_id for r in self.results()}, {"c001"})

    def test_lemonex_is_undetermined(self):
        results = self.results()
        args = ("c001", "2026-09-30", [c for r in results for c in r.criteria],
                [g for r in results for g in r.gates], [i for r in results for i in r.issues])
        strict = judge(*args, policy=STRICT_POLICY)
        self.assertEqual((strict.final_status, strict.total_score), (FinalStatus.UNDETERMINED, None))
        # 완화: 채점 가중치 80% → 부분 판정 적격
        relaxed = judge(*args, policy=PARTIAL_POLICY)
        self.assertEqual((relaxed.final_status, relaxed.total_score, relaxed.score_basis),
                         (FinalStatus.ELIGIBLE, 90.0, "partial"))
        # 기본값(0점 처리): C1=0 → 0+8+15+12+25+12 = 72
        zero = judge(*args)
        self.assertEqual((zero.final_status, zero.total_score, zero.score_basis),
                         (FinalStatus.ELIGIBLE, 72.0, "zero_filled"))
        review = strict
        self.assertEqual(review.reason_codes, [ReasonCode.MISSING_EVIDENCE])
        # C1은 기업별 N/A → unknown. 나머지는 채워져 참고 점수가 나온다
        self.assertEqual([u for u in review.remaining_unknowns if "미확인" in u or "not_checked" in u],
                         ["C1: 점수 미확인"])
        # C2 4, C3 5, C4 4, C5 5, C6 4 → (8+15+12+25+12) / 80 × 100 = 90
        self.assertEqual((review.reference_score, review.reference_weight), (90.0, 0.8))
        self.assertEqual(review.reference_criteria, ["C2", "C3", "C4", "C5", "C6"])
        self.assertEqual({g.code: g.status for g in review.gate_results},
                         {"G01": GateStatus.CLEAR, "G02": GateStatus.CLEAR})

    def test_scores_filled_gives_final_status(self):
        results = self.results()
        criteria = [c for r in results for c in r.criteria]
        for c in criteria:  # C1·C4만 채우면 판정까지 간다
            if c.criterion_id in ("C1", "C4"):
                c.score, c.score_status = 3.0, ScoreStatus.SCORED
        review = judge("c001", "2026-09-30", criteria, [g for r in results for g in r.gates])
        self.assertIn(review.final_status, (FinalStatus.ELIGIBLE, FinalStatus.INELIGIBLE))
        self.assertIsNotNone(review.total_score)

    def test_stub(self):
        r = not_run("clinical", "c009")
        self.assertEqual([c.criterion_id for c in r.criteria], ["C1"])
        self.assertEqual([(g.code, g.status) for g in r.gates], [("G01", GateStatus.NOT_CHECKED)])
        self.assertEqual([c.criterion_id for c in not_run("market", "c009").criteria], ["C2", "C3", "C4"])


if __name__ == "__main__":
    unittest.main()
