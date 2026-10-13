"""Orchestrator 테스트 (가짜 Worker, 외부 호출·LLM 없음).

    python -m pytest agents/orchestrator/test_orchestrator.py -q
"""
from __future__ import annotations

from typing import Any

import pytest
from agents.orchestrator.graph import build_company_graph, build_graph
from agents.orchestrator.routing import merge_datasets, plan_workers, slice_dataset

AS_OF = "2026-10-13"


@pytest.fixture(autouse=True)
def _dataset_store(tmp_path, monkeypatch):
    """데이터셋 저장소를 테스트마다 임시 폴더로 (store.ORCH_DATASET_STORE)."""
    monkeypatch.setenv("ORCH_DATASET_STORE", str(tmp_path / "store"))


def dataset(*kinds: str, untagged: int = 0) -> dict:
    sources, records = [], []
    for n, kind in enumerate(kinds):
        sid = f"s{n}"
        sources.append({"source_id": sid, "title": sid, "url": f"https://x/{sid}", "published_at": "2026-01-01"})
        rec: dict[str, Any] = {"record_id": f"r{n}", "source_id": sid, "content": f"{kind} 자료"}
        if kind == "market":
            rec["agent"] = "market"
        else:
            rec["tags"] = [kind]
        records.append(rec)
    for n in range(untagged):
        sources.append({"source_id": f"u{n}", "title": "u", "url": "https://u", "published_at": "2026-01-01"})
        records.append({"record_id": f"u{n}", "source_id": f"u{n}", "content": "표시 없음"})
    return {"version": "ds-1", "sources": sources, "records": records}


def company(cid: str = "c001", ds: dict | None = None) -> Any:
    return {"run_id": "t", "company_id": cid, "as_of": AS_OF, "company_profile": {"company_name": cid},
            "dataset": ds if ds is not None else dataset("clinical", "traction")}


def result(state: dict, agent: str, data: dict, evidence: list | None = None, findings: list | None = None,
           status: str = "complete", coverage: list | None = None) -> dict:
    prev = state.get("previous_result") or {}
    return {"run_id": state["run_id"], "company_id": state["company_id"], "as_of": state["as_of"],
            "schema_version": state["schema_version"], "criteria_version": state["criteria_version"],
            "agent": agent, "dataset_version": state["dataset"]["version"],
            "result_version": int(prev.get("result_version") or 0) + 1, "analysis_status": status, "data": data,
            "sources": [], "evidence": evidence or [], "findings": findings or [], "missing_items": [],
            "coverage": coverage or [], "review_responses": [], "errors": []}


class Fakes:
    """호출 기록을 남기는 가짜 Worker. traction은 첫 평가에서 근거 없는 점수를 내 보완 요청을 유도한다."""
    def __init__(self, traction_fixes: bool = True, fail: tuple[str, ...] = (), ask_all: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.traction_fixes, self.fail, self.ask_all = traction_fixes, fail, ask_all

    def clinical(self, s):
        self.calls.append(("clinical", s))
        if "clinical" in self.fail:
            raise RuntimeError("boom")
        c1 = {"score": 4, "score_status": "scored", "evidence_ids": ["ce1"]}
        return {"worker_results": {"clinical": result(
            s, "clinical", {"criteria_inputs": {"C1": c1}, "red_flags": []}, evidence=[{"evidence_id": "ce1"}],
            coverage=[{"area": "regulatory", "status": "reviewed"}])}}

    def traction(self, s):
        self.calls.append(("traction", s))
        fixed = bool(s.get("review_request")) and self.traction_fixes
        c5 = {"score": 3, "score_status": "scored", "evidence_ids": ["te1"] if fixed else []}
        res = result(s, "traction", {"criteria_inputs": {"C5": c5}, "red_flags": [], "open_questions": []},
                     evidence=[{"evidence_id": "te1"}],
                     findings=[{"finding_id": "tf1", "related_criterion_ids": ["C5"], "evidence_ids": ["te1"]}])
        if s.get("review_request"):
            req = s["review_request"]
            res["review_responses"] = [{"request_id": req["request_id"], "question_id": q["question_id"],
                                        "answer": "근거 연결", "finding_ids": ["tf1"], "resolved": fixed}
                                       for q in req["questions"]]
        return {"worker_results": {"traction": res}}

    def market(self, s):
        self.calls.append(("market", s))
        c2 = {"score": 3, "score_status": "scored", "evidence_ids": []} if self.ask_all else None
        return result(s, "market", {"criteria_inputs": {"C2": c2}, "dimensions": {}})   # market은 결과 자체를 반환

    def risk(self, s):
        self.calls.append(("risk", s))
        return {"worker_results": {"risk": result(s, "risk", {"areas": []}, status="partial")}}

    def workers(self):
        return {"clinical": self.clinical, "market": self.market, "traction": self.traction, "risk": self.risk}

    def names(self):
        return [n for n, _ in self.calls]


# ── 라우팅 ────────────────────────────────────────────────────────────────
def test_plan_selects_only_workers_with_data():
    p = plan_workers(dataset("clinical", "traction", "traction"), "c001")
    assert p["selected"] == ["clinical", "traction"]
    assert set(p["skipped"]) == {"market", "risk"}
    assert p["record_counts"] == {"clinical": 1, "market": 0, "traction": 2, "risk": 0}


def test_plan_recognizes_each_worker_marking():
    ds = dataset("market", "risk")
    ds["records"].append({"record_id": "m", "source_id": "s0", "content": "{}", "tags": ["area:regulatory"]})
    assert plan_workers(ds)["selected"] == ["clinical", "market", "risk"]


def test_plan_ignores_other_company_records():
    ds = dataset("risk")
    ds["records"][0]["company_ids"] = ["c999"]
    assert plan_workers(ds, "c001")["selected"] == []


def test_unmarked_dataset_runs_all_and_empty_runs_none():
    assert plan_workers(dataset(untagged=2))["selected"] == ["clinical", "market", "traction", "risk"]
    p = plan_workers({"version": "v", "sources": [], "records": []})
    assert p["selected"] == [] and p["skipped"]["risk"] == "이 기업 자료가 데이터셋에 없음"


def test_slice_keeps_own_records_sources_and_version():
    sl = slice_dataset(dataset("clinical", "traction", "market"), "traction")
    assert [r["record_id"] for r in sl["records"]] == ["r1"]
    assert [s["source_id"] for s in sl["sources"]] == ["s1"] and sl["version"] == "ds-1"


def test_merge_datasets_tags_unmarked_records():
    merged = merge_datasets([dataset("clinical"), {"sources": [{"source_id": "z"}],
                                                   "records": [{"record_id": "z1", "source_id": "z"}]}],
                            default_tags={1: ["risk"]})
    assert plan_workers(merged)["selected"] == ["clinical", "risk"] and merged["version"].startswith("merged-")


# ── 그래프 ────────────────────────────────────────────────────────────────
def test_dynamic_fan_out_and_review_loop():
    f = Fakes()
    out = build_company_graph(f.workers()).invoke(company())
    assert sorted(f.names()) == ["clinical", "traction", "traction"]          # market·risk 미실행, traction 보완 1회
    second = [s for n, s in f.calls if n == "traction"][1]
    req = second["review_request"]
    assert req["request_id"] == "c001:traction:rr1" and req["target_finding_ids"] == ["tf1"]
    assert req["dataset_version"] == "ds-1" and req["questions"][0]["question"] == req["questions"][0]["text"]
    assert second["previous_result"]["result_version"] == 1
    assert out["worker_results"]["traction"]["result_version"] == 2
    review = out["investment_review"]
    assert review["review_request_ids"] == ["c001:traction:rr1"]
    assert review["orchestration"]["plan"]["selected"] == ["clinical", "traction"]
    assert any("시장·사업성 미실행" in u for u in review["remaining_unknowns"])
    scores = {c["criterion_id"]: c["score"] for c in review["criterion_results"]}
    assert scores["C1"] == 4 and scores["C5"] == 3


def test_review_round_cap_stops_loop():
    f = Fakes(traction_fixes=False)
    out = build_company_graph(f.workers(), max_review_rounds=1).invoke(company())
    assert f.names().count("traction") == 2                                  # 고쳐지지 않아도 상한에서 멈춤
    assert out["investment_review"]["orchestration"]["review_history"][-1]["stopped_by_cap"] is True
    f0 = Fakes()
    build_company_graph(f0.workers(), max_review_rounds=0).invoke(company())
    assert f0.names().count("traction") == 1


def test_worker_exception_becomes_failed_without_review():
    f = Fakes(fail=("clinical",))
    out = build_company_graph(f.workers()).invoke(company())
    assert out["worker_results"]["clinical"]["analysis_status"] == "failed"
    assert out["worker_results"]["clinical"]["errors"][0]["code"] == "WORKER_EXCEPTION"
    assert f.names().count("clinical") == 1


def test_worker_receives_only_its_slice():
    f = Fakes()
    build_company_graph(f.workers()).invoke(company(ds=dataset("clinical", "traction", "market", "risk")))
    got = {n: [r["record_id"] for r in s["dataset"]["records"]] for n, s in f.calls if not s.get("review_request")}
    assert got == {"clinical": ["r0"], "traction": ["r1"], "market": ["r2"], "risk": ["r3"]}


def test_no_data_goes_straight_to_finalize():
    f = Fakes()
    out = build_company_graph(f.workers()).invoke(company(ds={"version": "v", "sources": [], "records": []}))
    assert f.calls == [] and out["investment_review"]["final_status"]


def test_run_graph_multiple_companies():
    f = Fakes()
    out = build_graph(f.workers()).invoke({"run_id": "t", "as_of": AS_OF, "companies": [
        {k: v for k, v in company("c001").items() if k not in ("run_id", "as_of")},
        {k: v for k, v in company("c002", dataset("risk")).items() if k not in ("run_id", "as_of")}]})
    plans = {r["company_id"]: r["orchestration"]["plan"]["selected"] for r in out["final_reviews"]}
    assert plans == {"c001": ["clinical", "traction"], "c002": ["risk"]}


def test_clinical_not_applicable_regulatory_counts_as_checked():
    from agents.orchestrator.graph import to_agent_result
    res = {"company_id": "c001", "as_of": AS_OF, "analysis_status": "complete", "data": {"red_flags": []},
           "coverage": [{"area": "regulatory", "status": "not_applicable"}, {"area": "regulatory", "status": "reviewed"}]}
    assert to_agent_result("clinical", res, "c001").gates[0].status.value == "clear"


# ── 노드 출력 → 다음 노드 입력 스키마 일치 ─────────────────────────────────
def test_payloads_match_worker_input_contracts():
    """Send로 보낸 입력이 각 Worker의 실제 입력 검증을 통과하는지 (최초 + 보완 요청 모두)."""
    from agents.market.contract import parse_input
    from agents.orchestrator.graph import WORKER_INPUT_KEYS
    from agents.risk.evaluator import _validate as risk_validate
    from agents.traction_growth.schema import CommonReviewRequest
    f = Fakes(ask_all=True)
    build_company_graph(f.workers()).invoke(company(ds=dataset("clinical", "traction", "market", "risk")))
    assert sorted(f.names()) == ["clinical", "market", "market", "risk", "risk", "traction", "traction"]
    for name, payload in f.calls:
        assert tuple(payload) == WORKER_INPUT_KEYS                  # WorkerInput 9개 키만
        if name == "market":
            parse_input(payload, "market")                          # extra="forbid" 계약
        if name == "traction" and payload["review_request"]:
            CommonReviewRequest.model_validate(payload["review_request"])
        if name == "risk":
            risk_validate(payload)                                  # risk 실제 입력 검증 (재검토 조건 포함)
        if name == "risk" and payload["review_request"]:
            req, prev = payload["review_request"], payload["previous_result"]
            assert isinstance(req["attempt"], int) and req["attempt"] >= 1
            assert req["dataset_version"] == payload["dataset"]["version"] == prev["dataset_version"]
            assert all(q["question_id"] and q["question"].strip() for q in req["questions"])
            assert set(req["target_finding_ids"]) <= {x["finding_id"] for x in prev["findings"]}
            for k in ("run_id", "company_id", "as_of", "schema_version", "criteria_version"):
                assert prev[k] == payload[k]


# ── State 설계: 제어/페이로드 · 지속성 비용 · 재개/복구 ───────────────────────
def test_control_and_payload_keys_are_separate():
    from agents.orchestrator.graph import CONTROL_KEYS, PAYLOAD_KEYS
    assert not set(CONTROL_KEYS) & set(PAYLOAD_KEYS)
    assert {"plan", "review_round", "review_requests"} <= set(CONTROL_KEYS)
    assert {"dataset_ref", "worker_results", "investment_review"} <= set(PAYLOAD_KEYS)


def test_dataset_moves_out_of_state_but_workers_still_get_their_slice(tmp_path):
    f = Fakes()
    out = build_company_graph(f.workers()).invoke(company(ds=dataset("clinical", "traction")))
    assert out["dataset"] is None and out["dataset_ref"]["records"] == 2
    from pathlib import Path
    assert Path(out["dataset_ref"]["uri"]).exists() and str(tmp_path) in out["dataset_ref"]["uri"]
    first = [s for n, s in f.calls if n == "traction"][0]
    assert [r["record_id"] for r in first["dataset"]["records"]] == ["r1"]       # Worker 계약: 본문 슬라이스
    second = [s for n, s in f.calls if n == "traction"][1]
    assert second["review_request"]["dataset_version"] == "ds-1"                 # 참조에서 version 유지


def test_checkpoints_do_not_carry_dataset_records():
    from langgraph.checkpoint.memory import InMemorySaver
    g = build_company_graph(Fakes().workers(), checkpointer=InMemorySaver())
    cfg: Any = {"configurable": {"thread_id": "t1"}}
    g.invoke(company(ds=dataset("clinical", "traction", "market", "risk")), cfg)
    history = list(g.get_state_history(cfg))
    after_plan = [h for h in history if h.values.get("plan")]
    assert after_plan and all(h.values.get("dataset") is None and h.values.get("dataset_ref") for h in after_plan)


class Crash(BaseException):
    """Worker 예외 처리(Exception)를 통과하는 중단 — 프로세스가 죽은 상황을 흉내 낸다."""


def test_resume_skips_finished_companies(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver
    import sqlite3

    from agents.orchestrator.store import externalize, invoke_resumable
    db = tmp_path / "ckpt.sqlite"
    companies = externalize([{k: v for k, v in company(cid).items() if k not in ("run_id", "as_of")}
                             for cid in ("c001", "c002")])
    cfg: Any = {"configurable": {"thread_id": "run-1"}, "max_concurrency": 1}
    inputs: Any = {"run_id": "t", "as_of": AS_OF, "companies": companies}

    f1 = Fakes()
    crash = {"on": True}
    def clinical(s):
        if s["company_id"] == "c002" and crash["on"]:
            raise Crash()
        return f1.clinical(s)
    g1 = build_graph({**f1.workers(), "clinical": clinical},
                     checkpointer=SqliteSaver(sqlite3.connect(str(db), check_same_thread=False)))
    with pytest.raises(Crash):
        g1.invoke(inputs, cfg)
    assert "c001" in {s["company_id"] for _, s in f1.calls}                    # c001은 끝났고 c002 도중 중단

    f2 = Fakes()                                       # 새 프로세스처럼: 새 그래프·새 연결, 같은 파일·thread_id
    g2 = build_graph(f2.workers(), checkpointer=SqliteSaver(sqlite3.connect(str(db), check_same_thread=False)))
    out = invoke_resumable(g2, inputs, cfg)
    assert {s["company_id"] for _, s in f2.calls} == {"c002"}                   # 끝난 c001은 다시 실행하지 않음
    assert sorted(r["company_id"] for r in out["final_reviews"]) == ["c001", "c002"]


def test_risk_common_result_read_directly_by_adapter():
    from agents.investment_review.validate import validate_result
    from agents.orchestrator.graph import to_agent_result
    obs = {"kind": "risk_signal", "statement": "핵심 인력 이탈", "citations": [{"passage_id": "p1"}]}
    res = {"company_id": "c001", "as_of": AS_OF, "analysis_status": "complete", "result_version": 1,
           "data": {"areas": [{"category": "people", "observations": [obs], "unknowns": [],
                               "due_diligence_questions": []}]},
           "sources": [{"source_id": "s1", "published_at": "2026-01-01"}],
           "evidence": [{"evidence_id": "c001:risk:ev001", "source_id": "s1"}],
           "findings": [{"finding_id": "f1", "category": "people", "evidence_ids": ["c001:risk:ev001"]}],
           "missing_items": []}
    ar = to_agent_result("risk", res, "c001")
    c6 = ar.criteria[0]
    assert c6.score == 3.0 and c6.evidence_ids == ["c001:risk:ev001"] and ar.source_ids == ["s1"]
    assert validate_result("c001", AS_OF, ar) == []                              # 근거 ID가 결과 안에 있음
