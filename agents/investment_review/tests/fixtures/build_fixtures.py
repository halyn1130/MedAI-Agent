"""02~05 실제 Agent 코드로 06 어댑터용 출력 샘플을 만든다. 유료 API는 호출하지 않는다.

    python -m agents.investment_review.tests.fixtures.build_fixtures

- clinical: agents/clinical_regulatory/clinical_results/의 실제 결과를 복사 (c001 레모넥스, c002 C1 채점 사례)
- market  : 실제 MarketAgent 그래프 + tests/test_market_agent.py의 가짜 검색·RAG·추론기 (내용은 가짜)
- traction: 실제 run_traction. OpenAI·Tavily·Naver 비활성, DART·국민연금(무료)만 사용
- risk    : 실제 RiskAgent 그래프 + 가짜 모델 응답 (내용은 가짜)

company_id는 통합 그래프처럼 01 정규화 ID(c001)를 모든 Agent에 넘긴다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[3]
CID, NAME, AS_OF = "c001", "레모넥스", "2026-09-30"


def write(name: str, payload: dict) -> None:
    (HERE / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  {name}")


def clinical() -> None:
    src = ROOT / "agents/clinical_regulatory/clinical_results"
    for cid in ("c001", "c002"):
        shutil.copy(src / f"{cid}.json", HERE / f"clinical_{cid}.json")
        print(f"  clinical_{cid}.json")


def market() -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    from test_market_agent import FakeReasoner, FakeRetriever, FakeSearch, complete_profile
    from agents.market.agent import MarketAgent
    from agents.market.schema import CheckStatus

    class PartialReasoner(FakeReasoner):
        """수익화 체크 1개를 unknown으로 바꿔 C4=null 사례를 만든다."""
        def analyze_dimension(self, dimension, *args, **kwargs):
            draft = super().analyze_dimension(dimension, *args, **kwargs)
            if dimension == "monetization" and draft.checks:
                draft.checks[1] = draft.checks[1].model_copy(
                    update={"status": CheckStatus.UNKNOWN, "rationale": "가격 조건 비공개", "evidence_ids": []})
            return draft

    profile = {**complete_profile(), "company_id": CID, "company_name": NAME}
    out = MarketAgent(FakeSearch(), FakeRetriever(), PartialReasoner()).run(profile, as_of=AS_OF, run_id="fixture")
    write("market_c001.json", out.model_dump(mode="json"))


def traction() -> None:
    for key in ("OPENAI_API_KEY", "TAVILY_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
        os.environ[key] = ""  # load_env()는 이미 있는 값을 덮어쓰지 않는다
    from agents.traction_growth.agent import profile_from_csv, run_traction

    profile = {**profile_from_csv(NAME), "company_id": CID}
    write("traction_c001.json", run_traction(profile, as_of=AS_OF, run_id="fixture", llm=None))


def risk() -> None:
    from agents.risk import RiskAgent
    from agents.risk.company_analysis import CATS

    state = {"as_of": AS_OF, "company_profile": {"company_id": CID, "company_name": NAME}, "company_evidence": [
        {"source_id": "s1", "url": "https://example.com/fixture-1", "title": "[fixture] 협약 기사",
         "published_at": "2026-01-01", "content": f"{NAME}는 병원과 공동연구 협약을 발표했습니다. " + "fixture 문장입니다. " * 10},
        {"source_id": "s2", "url": "https://example.com/fixture-2", "title": "[fixture] 경영진 기사",
         "published_at": "2026-03-01", "content": f"{NAME}의 최고기술책임자가 교체되었다고 밝혔습니다. " + "fixture 문장입니다. " * 10}]}

    def caller(payload):
        pids = [p["passage_id"] for p in payload["passages"]]
        areas = [{"category": c, "observations": [], "unknowns": [f"[fixture] {c} 세부 조건 미확인"],
                  "questions": [f"[fixture] {c} 관련 계약·승계 조건은 무엇인가?"]} for c in CATS]
        areas[0]["observations"] = [{"statement": "[fixture] 병원과 공동연구 협약 발표", "passage_ids": pids[:1],
                                     "kind": "observation", "conditional_impact": None}]
        areas[1]["observations"] = [{"statement": "[fixture] 최고기술책임자 교체", "passage_ids": pids[1:2],
                                     "kind": "risk_signal", "conditional_impact": "후임 체계가 없으면 개발 일정 지연 가능"}]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"areas": areas})}}]}

    with tempfile.TemporaryDirectory() as d:
        root, raw = Path(d), json.dumps(state, ensure_ascii=False).encode()
        (root / "state.json").write_bytes(raw)
        (root / "manifest.json").write_text(json.dumps(
            {"companies": {CID: {"path": "state.json", "sha256": hashlib.sha256(raw).hexdigest()}}}))
        out = RiskAgent(manifest_path=root / "manifest.json", cache_dir=root / "cache",
                        model="fixture", caller=caller)({"company_profile": {"company_id": CID}})
    write("risk_c001.json", out)


if __name__ == "__main__":
    for step in (clinical, market, traction, risk):
        print(step.__name__)
        step()
