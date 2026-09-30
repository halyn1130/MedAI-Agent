"""
AGENT 02 / CLINICAL & REGULATORY - 임상·인허가 분석 (v2.0)

제품의 규제 적용 범위, 공식 상태, 임상 근거와 회사 주장의 일치 여부를 제품·국가별로 확인한다.
최종 투자 여부는 결정하지 않는다.

──────────────────────────────────────────────
전체 그래프 연결 (오케스트레이터)
──────────────────────────────────────────────
등록:  main.add_node("clinical", clinical_analysis_node)

읽기
- company_id + company_profile        : 기업 1곳 실행 (기업별 State)
  또는 companies {company_id: profile} : 여러 기업 일괄 실행 (동시 5개)
- clinical_analysis {company_id: Envelope} : 이전 결과 (보완 시 사용)
- review_requests [ReviewRequest]    : target_agent == "clinical" 인 것만 처리
- run_id, as_of                      : 공통 실행 정보 (없으면 기본값)

쓰기
- clinical_analysis {company_id: Envelope}   → dict 병합 Reducer 필요
- source_registry  {source_id: Source}       → 공통 출처 레지스트리 병합

처리 순서 (서브그래프)
0.collect(CSV만 입력 시 자체 수집) → prepare → 1.classify → 2.regulatory → 3.institutional → 4.studies → 5.claims
        → score(C1·레드플래그·Finding·결측) → 6.validate → respond(보완 시) → assemble
"""
from __future__ import annotations

import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Optional

from langgraph.graph import END, START, StateGraph

try:
    from . import schema as S
    from .config import AGENT, COMPANY_CONCURRENCY, MAX_REVIEW_ROUNDS, RESULT_DIR
    from .context import ClinicalAgentState, ClinicalRun, normalize_profile
    from .nodes import (
        assemble_node,
        claims_node,
        classify_node,
        collect_node,
        institutional_node,
        prepare_node,
        regulatory_node,
        respond_node,
        score_node,
        studies_node,
        validate_node,
    )
    from .utils import today
except ImportError:
    import schema as S
    from config import AGENT, COMPANY_CONCURRENCY, MAX_REVIEW_ROUNDS, RESULT_DIR
    from context import ClinicalAgentState, ClinicalRun, normalize_profile
    from nodes import (
        assemble_node,
        claims_node,
        classify_node,
        collect_node,
        institutional_node,
        prepare_node,
        regulatory_node,
        respond_node,
        score_node,
        studies_node,
        validate_node,
    )
    from utils import today


# ══════════════════════════════════════════════
# LangGraph 서브그래프 구성
# ══════════════════════════════════════════════
@lru_cache(maxsize=1)
def build_clinical_graph():
    """임상·인허가 분석 LangGraph 워크플로우 빌드"""
    g = StateGraph(ClinicalAgentState)

    # 1. 노드 등록
    for name, fn in [
        ("collect", collect_node),
        ("prepare", prepare_node),
        ("classify", classify_node),
        ("regulatory", regulatory_node),
        ("institutional", institutional_node),
        ("studies", studies_node),
        ("claims", claims_node),
        ("score", score_node),
        ("validate", validate_node),
        ("respond", respond_node),
        ("assemble", assemble_node),
    ]:
        g.add_node(name, fn)

    # 2. 엣지 연결 (순차 파이프라인)
    order = [
        "collect",
        "prepare",
        "classify",
        "regulatory",
        "institutional",
        "studies",
        "claims",
        "score",
        "validate",
        "respond",
        "assemble",
    ]
    g.add_edge(START, order[0])
    for a, b in zip(order, order[1:]):
        g.add_edge(a, b)
    g.add_edge(order[-1], END)

    return g.compile()


# ══════════════════════════════════════════════
# 기업 1곳 실행
# ══════════════════════════════════════════════
def _failed_envelope(company_id: str, as_of: str, run_id: str, previous: Optional[dict], error: str) -> dict:
    prev = previous or {}
    env = S.AnalysisEnvelope(
        run_id=run_id, company_id=company_id, as_of=as_of, agent=AGENT,
        result_version=int(prev.get("result_version", 0)) + 1, analysis_status="failed",
        data=S.ClinicalData(clinical_summary=f"분석 실패: {error}", validation_issues=[error]).model_dump()
        if not prev else prev.get("data", {}),
        sources=prev.get("sources", []), evidence=prev.get("evidence", []), findings=prev.get("findings", []),
        missing_items=prev.get("missing_items", []), coverage=prev.get("coverage", []),
        review_responses=prev.get("review_responses", []),
    )
    return env.model_dump()


def run_company(company_id: str, raw_profile: dict, as_of: str, run_id: str,
                previous: Optional[dict], raw_requests: list[dict]) -> dict:
    try:
        collected = ((previous or {}).get("data") or {}).get("collected_profile") or {}
        company = normalize_profile(company_id, {**raw_profile, **collected})
        reqs, over = [], []
        for r in raw_requests:
            try:
                req = S.ReviewRequest(**r)
            except Exception:
                continue  # 형식 불일치 요청 무시
            (reqs if 1 <= req.review_round <= MAX_REVIEW_ROUNDS else over).append(req)
        ctx = ClinicalRun(company, as_of, run_id, previous, reqs, over)
        ctx.raw_profile = raw_profile
        out = build_clinical_graph().invoke({"ctx": ctx})
        return out["envelope"]
    except Exception as e:
        return _failed_envelope(company_id, as_of, run_id, previous, f"{type(e).__name__}: {e}"[:300])


# ══════════════════════════════════════════════
# 전체 그래프에 등록할 노드 함수 (오케스트레이터 연동)
# ══════════════════════════════════════════════
def _save_company_result(cid: str, env: dict) -> None:
    if not RESULT_DIR:
        return
    d = Path(RESULT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.json").write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")


def load_saved_results(result_dir: str) -> dict[str, dict]:
    d = Path(result_dir)
    if not d.exists():
        return {}
    out = {}
    for f in d.glob("*.json"):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            failed_search = any(c.get("status") == "search_failed" for c in env.get("coverage", []))
            if env.get("analysis_status") != "failed" and not failed_search:  # 실패·조회 실패 기업은 다시 분석
                out[f.stem] = env
        except Exception:
            continue
    return out


def clinical_analysis_node(state: dict) -> dict:
    as_of = state.get("as_of") or today()
    run_id = state.get("run_id") or f"run-{as_of}"
    if state.get("company_id") and state.get("company_profile"):
        companies = {state["company_id"]: state["company_profile"]}
    else:
        companies = state.get("companies") or {}
    prev_all: dict = state.get("clinical_analysis") or {}
    requests_all = [r for r in (state.get("review_requests") or []) if r.get("target_agent") == AGENT]

    jobs = []
    for cid, profile in companies.items():
        prev = prev_all.get(cid)
        answered = {rr["request_id"] for rr in (prev or {}).get("review_responses", [])}
        reqs = [r for r in requests_all if r.get("company_id") == cid and r.get("request_id") not in answered]
        if prev and not reqs:
            continue  # 이미 분석했고 새 요청 없음
        jobs.append((cid, profile, prev, reqs))

    updates: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=COMPANY_CONCURRENCY) as pool:
        futures = {pool.submit(run_company, cid, prof, as_of, run_id, prev, reqs): cid
                   for cid, prof, prev, reqs in jobs}
        for fut, cid in futures.items():
            updates[cid] = fut.result()
            print(f"[clinical] {cid} → {updates[cid]['analysis_status']} (v{updates[cid]['result_version']})")
            _save_company_result(cid, updates[cid])

    registry = {s["source_id"]: s for env in updates.values() for s in env["sources"]}
    return {"clinical_analysis": updates, "source_registry": registry}


# ══════════════════════════════════════════════
# 단독 테스트: python agent.py startup_list.csv 3
# ══════════════════════════════════════════════
def load_companies_from_csv(path: str) -> dict[str, dict]:
    companies: dict[str, dict] = {}
    seen: dict[str, str] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("기업명") or "").strip()
            if not name:
                continue
            cid = seen.get(name) or f"c{len(seen) + 1:03d}"
            seen[name] = cid
            companies[cid] = row  # 중복 기업은 마지막 행 사용
    return companies


def print_check(envs: dict[str, dict]) -> None:
    """실행 결과 점검용 요약 출력"""
    for cid, env in envs.items():
        d, cp = env["data"], env["data"].get("collected_profile", {})
        cov = {}
        for c in env["coverage"]:
            cov.setdefault(c["area"], set()).add(c["status"])
        print(f"\n=== {cid} | {env['analysis_status']} ===")
        print("  정식명:", cp.get("legal_name"), "| 홈페이지:", cp.get("homepage"))
        print("  제품:", [(p.get("name"), p.get("model"), f"주장 {len(p.get('claims', []))}건") for p in cp.get("products", [])])
        print("  허가:", [(r["country"], r["procedure_type"], r["status"], r.get("record_number")) for r in d["regulatory_records"]])
        print("  C1:", d.get("criteria_inputs", {}).get("C1", {}).get("score"), "| 플래그:", [f["code"] for f in d["red_flags"]])
        print("  검토 상태:", {k: sorted(v) for k, v in cov.items()})
        if d["validation_issues"]:
            print("  검증 메모:", d["validation_issues"][:3])


if __name__ == "__main__":
    # 사용: python agent.py startup_list.csv [개수] [기업명,기업명,...]
    #  - 기업별 결과를 clinical_results/ 에 바로 저장하고, 다시 실행하면 끝난 기업은 건너뜀
    #  - 처음부터 다시 하려면 clinical_results/ 폴더를 지우고 실행
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "startup_list.csv"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    names = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    if not RESULT_DIR:
        RESULT_DIR = "clinical_results"
    companies = load_companies_from_csv(csv_path)
    if names:
        companies = {cid: row for cid, row in companies.items() if row.get("기업명") in names}
    companies = dict(list(companies.items())[:limit])

    force = "--force" in sys.argv or "--rerun" in sys.argv or os.getenv("RERUN", "0").lower() in ("1", "true")
    if force:
        done = {}
    elif names:
        done = {cid: env for cid, env in load_saved_results(RESULT_DIR).items() if cid in companies and companies[cid].get("기업명") not in names}
    else:
        done = {cid: env for cid, env in load_saved_results(RESULT_DIR).items() if cid in companies}
    if done:
        print(f"이미 분석된 {len(done)}개 기업은 건너뜀 ({RESULT_DIR}/)")
    result = clinical_analysis_node({"companies": companies, "clinical_analysis": done})
    merged = {**done, **result["clinical_analysis"]}
    merged = {cid: merged[cid] for cid in companies if cid in merged}  # CSV 순서대로

    out_path = Path("clinical_analysis.json")
    out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    # print_check(result["clinical_analysis"])
    print(f"\n완료: 전체 {len(merged)}개 기업 (이번 실행 {len(result['clinical_analysis'])}개) → {out_path}")