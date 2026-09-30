"""팀원 원본 출력 → 읽기 → 검증 → 보완 요청 → 판정 → 선정을 이어서 확인한다.

레모넥스 샘플(팀원 원본 JSON)을 복사·수정해 가상 기업을 만든다.
06 형식으로 바꾼 뒤가 아니라 원본을 고치므로 어댑터부터 전부 거친다.
"""
import copy
import json
import unittest
from pathlib import Path

from ..adapters import adapt_clinical, adapt_market, adapt_risk, adapt_traction
from ..contract import FinalStatus, ReasonCode
from ..judge import judge
from ..policy import PARTIAL_POLICY, STRICT_POLICY
from ..review_requests import build_review_requests
from ..selection import select
from ..validate import validate

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = "2026-09-30"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


BASE = {"clinical": load("clinical_c001.json"), "market": load("market_c001.json")["market_analysis"],
        "traction": load("traction_c001.json"), "risk": load("risk_c001.json")}


def company(cid, c1=3.0, c2=None, c3=None, c4=3.0, c5=None, risk_signals=None, cl02=None, as_of=None):
    """원본 출력 4개를 복사해 company_id와 점수 입력을 바꾼다. None이면 샘플 값 유지."""
    raw = copy.deepcopy(BASE)
    old = "c001"
    raw = json.loads(json.dumps(raw).replace(f'"{old}', f'"{cid}').replace(f'{old}:', f'{cid}:'))
    ci = raw["clinical"]["data"]["criteria_inputs"]["C1"]
    if c1 is not None:
        raw["clinical"]["evidence"] = [{**raw["traction"]["evidence"][0], "evidence_id": f"{cid}:clinical:ev01"}]
        ci.update(score=c1, score_status="scored", evidence_ids=[f"{cid}:clinical:ev01"])
    if c2 is not None:
        raw["market"]["data"]["criteria_inputs"]["C2"].update(score=c2, score_status="scored")
    dims = raw["market"]["data"]["dimensions"]
    for names, value in ((("demand", "commercialization"), c3), (("monetization",), c4)):
        if value is not None:  # 06은 체크를 직접 센다 → yes value개, 나머지 no
            for name in names:
                for n, chk in enumerate(dims[name]["checks"]):
                    chk["status"] = "yes" if n < value else "no"
    if c5 is not None:
        raw["traction"]["data"]["criteria_inputs"]["C5"]["score"] = c5
    if risk_signals is not None:  # 영역별 위험 신호 수 → C6
        for area, n in zip(raw["risk"]["risk_analysis"]["areas"], risk_signals):
            base_obs = raw["risk"]["risk_analysis"]["areas"][0]["observations"][0]
            area["observations"] = [base_obs] + [{**base_obs, "kind": "risk_signal"} for _ in range(n)]
    if cl02:
        raw["clinical"]["data"]["red_flags"] = [{"flag_id": "f1", "finding_id": "fd1", "code": "CL02", "status": cl02,
                                                 "materiality": "high", "description": "목표국 판매 중지 명령",
                                                 "evidence_ids": []}]
    if as_of:
        raw["traction"]["as_of"] = as_of
    return cid, raw


def adapt(raw):
    return [adapt_clinical(raw["clinical"]), adapt_market(raw["market"]), adapt_traction(raw["traction"]),
            adapt_risk(raw["risk"]["risk_analysis"], raw["risk"]["references"])]


def review_company(cid, raw, review_round=0, policy=None):
    results = adapt(raw)                                        # 1. 읽기
    issues = validate(cid, AS_OF, results)                      # 2. 검증
    requests = build_review_requests(cid, AS_OF, issues, results, review_round)  # 3. 보완 요청
    criteria = [c for r in results for c in r.criteria]
    gates = [g for r in results for g in r.gates]
    review = judge(cid, AS_OF, criteria, gates, issues, **({"policy": policy} if policy else {}))  # 4. 판정
    return review, requests


class PipelineTests(unittest.TestCase):
    def test_lemonex_as_is(self):
        """샘플 그대로: C1(N/A)·C4 비어 있음 → 판단불가, 임상·시장에 보완 요청."""
        review, requests = review_company("c001", BASE, policy=STRICT_POLICY)
        self.assertEqual((review.final_status, review.total_score), (FinalStatus.UNDETERMINED, None))
        self.assertEqual(sorted(requests), ["clinical", "market"])
        # 완화 기준: C1만 비어 가중치 80% → 부분 판정
        review, _ = review_company("c001", BASE, policy=PARTIAL_POLICY)
        self.assertEqual((review.final_status, review.score_basis), (FinalStatus.ELIGIBLE, "partial"))
        # 기본값(0점 처리)
        review, _ = review_company("c001", BASE)
        self.assertEqual((review.final_status, review.total_score, review.score_basis),
                         (FinalStatus.ELIGIBLE, 72.0, "zero_filled"))

    def test_review_round_then_final(self):
        """보완 응답으로 C1·C4가 채워졌다고 가정 → 2회차에서 판정, 추가 요청 없음."""
        cid, raw = company("c101", c1=3.0, c4=3.0)
        review, requests = review_company(cid, raw, review_round=1)
        # C1 3, C2 4, C3 5, C4 3, C5 5, C6 4 → 12+8+15+9+25+12 = 81
        self.assertEqual((review.final_status, review.total_score), (FinalStatus.ELIGIBLE, 81.0))
        self.assertEqual(requests, {})

    def test_scenarios_and_selection(self):
        cases = [
            company("c101"),                                        # 12+8+15+9+25+12 = 81 적격
            company("c102", c1=4.0),                                # 16+8+15+9+25+12 = 85 적격
            company("c103", c1=4.0, c5=4.0),                        # 16+8+15+9+20+12 = 80 적격
            company("c104", c1=3.0, c5=4.0, c2=5.0, c3=4.0),       # 12+10+12+9+20+12 = 75 적격
            company("c105", c1=2.0, c4=2.0, c5=4.0, c3=3.0, risk_signals=[1, 1, 1]),  # 8+8+9+6+20+9 = 60 적격 (경계)
            company("c106", c1=2.0, c4=2.0, c5=2.0, c3=2.0, c2=2.0, risk_signals=[1, 1, 1]),  # 8+4+6+6+10+9 = 43 부적격
            company("c107", c1=1.0),                                # 4+8+15+9+25+12 = 73, C1<2 부적격
            company("c108", cl02="confirmed"),                      # G01 차단 부적격
            company("c109", cl02="candidate"),                      # G01 미해결 판단불가
            company("c110", as_of="2026-01-01"),                    # 기준일 불일치 판단불가
            company("c111", c1=4.0, c4=4.0),                        # 16+8+15+12+25+12 = 88 적격 (1위)
        ]
        reviews = {}
        for cid, raw in cases:
            reviews[cid], _ = review_company(cid, raw, review_round=1)

        expect = {
            "c101": (FinalStatus.ELIGIBLE, 81.0, []),
            "c102": (FinalStatus.ELIGIBLE, 85.0, []),
            "c103": (FinalStatus.ELIGIBLE, 80.0, []),
            "c104": (FinalStatus.ELIGIBLE, 75.0, []),
            "c105": (FinalStatus.ELIGIBLE, 60.0, []),
            "c106": (FinalStatus.INELIGIBLE, 43.0, [ReasonCode.SCORE_BELOW_60]),
            "c107": (FinalStatus.INELIGIBLE, 73.0, [ReasonCode.CRITERION_BELOW_2]),
            "c108": (FinalStatus.INELIGIBLE, 81.0, [ReasonCode.G01]),
            "c109": (FinalStatus.UNDETERMINED, None, [ReasonCode.MISSING_EVIDENCE]),
            "c110": (FinalStatus.UNDETERMINED, None, [ReasonCode.MISSING_EVIDENCE]),
            "c111": (FinalStatus.ELIGIBLE, 88.0, []),
        }
        for cid, (status, total, codes) in expect.items():
            r = reviews[cid]
            self.assertEqual((r.final_status, r.total_score, r.reason_codes), (status, total, codes), cid)

        out = select(reviews.values())                               # 5. 선정 (K=5)
        ranked = [(r.company_id, r.selection.rank, r.selection.selected) for r in out]
        self.assertEqual(ranked[:6], [("c111", 1, True), ("c102", 2, True), ("c101", 3, True),
                                      ("c103", 4, True), ("c104", 5, True), ("c105", 6, False)])
        self.assertEqual(len(out), 11)  # 부적격·판단불가 기업도 빠지지 않음
        self.assertTrue(all(r.selection.rank is None for r in out[6:]))


if __name__ == "__main__":
    unittest.main()
