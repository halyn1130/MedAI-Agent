import unittest

from ..contract import CriterionResult, FinalStatus, GateResult
from ..judge import judge
from ..policy import CRITERIA, Policy
from ..selection import select

CLEAR = [GateResult(code="G01", status="clear"), GateResult(code="G02", status="clear")]


def review(company_id, *scores, gates=CLEAR):
    items = [CriterionResult(criterion_id=c, score=s, score_status="scored" if s is not None else "unknown")
             for c, s in zip(CRITERIA, scores)]
    return judge(company_id, "2026-09-30", items, gates)


def ranked(results):
    return [(r.company_id, r.selection.rank, r.selection.selected) for r in results]


class SelectionTests(unittest.TestCase):
    def test_no_eligible(self):
        out = select([review("b", 2, 2, 2, 2, 2, 2), review("a", 4, None, 3, 4, 4, 3)])
        self.assertEqual(ranked(out), [("a", None, False), ("b", None, False)])
        self.assertEqual(out[0].selection.reason, "판단불가 (MISSING_EVIDENCE)")
        self.assertEqual(out[1].selection.reason, "부적격 (SCORE_BELOW_60)")

    def test_fewer_than_k_selects_all_eligible(self):
        out = select([review("a", 4, 3, 3.5, 4, 4, 3), review("b", 5, 5, 5, 5, 5, 5)])
        self.assertEqual(ranked(out), [("b", 1, True), ("a", 2, True)])

    def test_more_than_k(self):
        out = select([review(f"c{i}", 4, 4, 4, 4, 4, i % 2 + 3) for i in range(4)], k=2)
        self.assertEqual([s for _, _, s in ranked(out)], [True, True, False, False])
        self.assertIn("K=2 초과", out[2].selection.reason)

    def test_default_k_is_5(self):
        out = select([review(f"c{i}", 4, 4, 4, 4, 4, 4) for i in range(7)])
        self.assertEqual(sum(r.selection.selected for r in out), 5)

    def test_tiebreak_c5_then_c1_then_company_id(self):
        # 모두 총점 80. z는 C5가 가장 높고, c는 C5가 a·b와 같지만 C1이 높다. a·b는 완전 동점.
        z = review("z", 4, 4, 4, 4, 4.6, 3)
        c = review("c", 4.75, 4, 4, 4, 4, 3)
        a = review("a", 4, 4, 4, 4, 4, 4)
        b = review("b", 4, 4, 4, 4, 4, 4)
        self.assertEqual({r.total_score for r in (z, c, a, b)}, {80.0})
        out = select([b, a, c, z])
        self.assertEqual([r.company_id for r in out], ["z", "c", "a", "b"])

    def test_tiebreak_skips_population_not_applicable(self):
        policy = Policy(not_applicable=frozenset({"C1"}))
        items = lambda cid: judge(cid, "2026-09-30", [
            CriterionResult(criterion_id=c, score=4, score_status="scored") for c in CRITERIA[1:]], CLEAR, policy=policy)
        out = select([items("b"), items("a")], policy)
        self.assertEqual(ranked(out), [("a", 1, True), ("b", 2, True)])

    def test_gate_blocked_not_selected_even_with_high_score(self):
        gates = [GateResult(code="G01", status="confirmed"), GateResult(code="G02", status="clear")]
        out = select([review("a", 5, 5, 5, 5, 5, 5, gates=gates)])
        self.assertEqual(out[0].final_status, FinalStatus.INELIGIBLE)
        self.assertFalse(out[0].selection.selected)

    def test_duplicate_company_rejected(self):
        with self.assertRaises(ValueError):
            select([review("a", 4, 4, 4, 4, 4, 4), review("a", 4, 4, 4, 4, 4, 4)])


if __name__ == "__main__":
    unittest.main()
