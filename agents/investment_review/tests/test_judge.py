import unittest

from ..contract import (CriterionResult, FinalStatus, GateResult, IssueKind, ReasonCode,
                        ScoreStatus, ValidationIssue)
from ..judge import judge
from ..policy import CRITERIA, Policy
from ..scoring import normalize_criteria, total_score

CLEAR = [GateResult(code="G01", status="clear"), GateResult(code="G02", status="clear")]


def criteria(*scores):
    return [CriterionResult(criterion_id=c, score=s, score_status="scored" if s is not None else "unknown")
            for c, s in zip(CRITERIA, scores)]


def run(*scores, gates=CLEAR, **kw):
    return judge("c1", "2026-09-30", criteria(*scores), gates, **kw)


class ReadmeExampleTests(unittest.TestCase):
    """README 「7. 계산·판정 예시」"""

    def test_normal_pass(self):
        r = run(4, 3, 3.5, 4, 4, 3)
        self.assertEqual((r.total_score, r.final_status), (73.5, FinalStatus.ELIGIBLE))
        self.assertEqual(r.reason_codes, [])

    def test_exact_boundary_60_is_eligible(self):
        r = run(2, 3, 3, 4, 3.8, 2)
        self.assertEqual((r.total_score, r.final_status), (60.0, FinalStatus.ELIGIBLE))

    def test_criterion_below_2(self):
        r = run(1, 5, 5, 5, 5, 5)
        self.assertEqual((r.total_score, r.final_status), (84.0, FinalStatus.INELIGIBLE))
        self.assertEqual(r.reason_codes, [ReasonCode.CRITERION_BELOW_2])

    def test_total_below_60(self):
        r = run(2, 2, 2, 2, 2, 2)
        self.assertEqual((r.total_score, r.final_status), (40.0, FinalStatus.INELIGIBLE))
        self.assertEqual(r.reason_codes, [ReasonCode.SCORE_BELOW_60])

    def test_unknown_score_is_undetermined(self):
        r = run(4, 3, 3.5, None, 4, 3)
        self.assertEqual((r.total_score, r.final_status), (None, FinalStatus.UNDETERMINED))
        self.assertEqual(r.reason_codes, [ReasonCode.MISSING_EVIDENCE])
        self.assertIn("C4: 점수 미확인", r.remaining_unknowns)

    def test_confirmed_gate_overrides_score(self):
        gates = [GateResult(code="G01", status="confirmed"), GateResult(code="G02", status="clear")]
        r = run(4, 3, 3.5, 4, 4, 3, gates=gates)
        self.assertEqual((r.total_score, r.final_status), (73.5, FinalStatus.INELIGIBLE))
        self.assertEqual(r.reason_codes, [ReasonCode.G01])


class PriorityTests(unittest.TestCase):
    def test_confirmed_gate_beats_unknown_score(self):
        gates = [GateResult(code="G01", status="clear"), GateResult(code="G02", status="confirmed")]
        r = run(4, None, 3, 4, 4, 3, gates=gates)
        self.assertEqual((r.final_status, r.reason_codes, r.total_score),
                         (FinalStatus.INELIGIBLE, [ReasonCode.G02], None))

    def test_missing_gate_is_not_checked(self):
        r = run(4, 3, 3.5, 4, 4, 3, gates=[GateResult(code="G01", status="clear")])
        self.assertEqual((r.final_status, r.total_score), (FinalStatus.UNDETERMINED, None))
        self.assertIn("G02: not_checked", r.remaining_unknowns)

    def test_unresolved_gate_is_undetermined(self):
        gates = [GateResult(code="G01", status="unresolved"), GateResult(code="G02", status="clear")]
        self.assertEqual(run(4, 3, 3.5, 4, 4, 3, gates=gates).final_status, FinalStatus.UNDETERMINED)

    def test_blocking_issue_reason_codes(self):
        issues = [ValidationIssue(issue_id="v1", kind=IssueKind.CONFLICT, blocking=True),
                  ValidationIssue(issue_id="v2", kind=IssueKind.ANALYSIS_FAILED, blocking=True),
                  ValidationIssue(issue_id="v3", kind=IssueKind.MISSING_EVIDENCE, blocking=False)]
        r = run(4, 3, 3.5, 4, 4, 3, issues=issues)
        self.assertEqual(r.final_status, FinalStatus.UNDETERMINED)
        self.assertEqual(r.reason_codes, [ReasonCode.UNRESOLVED_CONFLICT, ReasonCode.ANALYSIS_FAILED])

    def test_both_score_rules_fail(self):
        r = run(1, 2, 2, 2, 2, 2)
        self.assertEqual(r.reason_codes, [ReasonCode.SCORE_BELOW_60, ReasonCode.CRITERION_BELOW_2])

    def test_just_below_boundaries(self):
        self.assertEqual(run(2, 3, 3, 4, 3.79, 2).final_status, FinalStatus.INELIGIBLE)
        self.assertEqual(run(1.99, 5, 5, 5, 5, 5).reason_codes, [ReasonCode.CRITERION_BELOW_2])


class ScoringTests(unittest.TestCase):
    def test_missing_criterion_becomes_unknown(self):
        results = normalize_criteria([CriterionResult(criterion_id="C5", score=4, score_status="scored")])
        self.assertEqual([r.criterion_id for r in results], list(CRITERIA))
        self.assertIsNone(total_score(results))

    def test_company_level_not_applicable_is_not_reweighted(self):
        items = criteria(4, 3, 3.5, 4, 4, 3)
        items[0] = CriterionResult(criterion_id="C1", score_status="not_applicable")
        results = normalize_criteria(items)
        self.assertEqual(results[0].score_status, ScoreStatus.UNKNOWN)
        self.assertIsNone(total_score(results))

    def test_population_not_applicable_reweights(self):
        policy = Policy(not_applicable=frozenset({"C1"}))
        r = judge("c1", "2026-09-30", criteria(None, 5, 5, 5, 5, 5), CLEAR, policy=policy)
        self.assertEqual((r.total_score, r.final_status), (100.0, FinalStatus.ELIGIBLE))
        self.assertEqual(r.criterion_results[0].score_status, ScoreStatus.NOT_APPLICABLE)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            CriterionResult(criterion_id="C1", score=6, score_status="scored")
        with self.assertRaises(ValueError):
            CriterionResult(criterion_id="C1", score=3, score_status="unknown")
        with self.assertRaises(ValueError):
            CriterionResult(criterion_id="C7")
        with self.assertRaises(ValueError):
            normalize_criteria(criteria(4, 3) + criteria(4))
        with self.assertRaises(ValueError):
            Policy(not_applicable=frozenset(CRITERIA))


if __name__ == "__main__":
    unittest.main()
