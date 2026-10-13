"""
WORKER / CLINICAL & REGULATORY - 임상·인허가 평가 노드 (v3.0)

제품의 규제 적용 범위, 공식 상태, 임상 근거와 회사 주장의 일치 여부를 제품·국가별로 평가한다.
외부 조회·재수집·보완 루프는 하지 않는다. 전달받은 데이터셋으로 한 번 평가해 반환하고,
실행 대상 선택·재실행·재수집은 Orchestrator가 결과(missing_items · coverage · errors)를 보고 결정한다.

──────────────────────────────────────────────
입출력 (공통 State 및 Worker 입출력 계약 초안)
──────────────────────────────────────────────
등록:  main.add_node("clinical", clinical_analysis_node)

읽기
- run_id, company_id, as_of, schema_version, criteria_version
- company_profile : legal_name, aliases, products[product_id, name, intended_use, claims 등]
- dataset         : {version, sources[Source], records[DataRecord]}
- previous_result : 재평가 시 이 Worker의 이전 결과 (없으면 worker_results["clinical"])
- review_request  : 재검토 요청 (없으면 review_requests["clinical"])

쓰기
- {"worker_results": {"clinical": WorkerResult}}

평가 순서 (LLM 호출은 1~5단계에서 각 1회, 재검토 요청이 있으면 답변 1회 추가)
1.classify → 2.regulatory → 3.institutional → 4.studies → 5.claims
        → score(C1·레드플래그·Finding·부족 정보) → 6.validate → respond(재검토 질문 답변) → assemble
"""
from __future__ import annotations

import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

try:
    from . import schema as S
    from .config import AGENT, COMPANY_CONCURRENCY, RESULT_DIR
    from .context import ClinicalRun, normalize_profile
    from .steps import (
        assemble,
        claims_step,
        classify_step,
        institutional_step,
        regulatory_step,
        respond_step,
        score_step,
        studies_step,
        validate_step,
    )
    from .utils import today
except ImportError:
    import schema as S
    from config import AGENT, COMPANY_CONCURRENCY, RESULT_DIR
    from context import ClinicalRun, normalize_profile
    from steps import (
        assemble,
        claims_step,
        classify_step,
        institutional_step,
        regulatory_step,
        respond_step,
        score_step,
        studies_step,
        validate_step,
    )
    from utils import today

STEPS = (classify_step, regulatory_step, institutional_step, studies_step, claims_step, score_step, validate_step,
         respond_step)


# ══════════════════════════════════════════════
# 기업 1곳 평가
# ══════════════════════════════════════════════
def evaluate_company(company_id: str, company_profile: dict, dataset: dict, as_of: str, run_id: str,
                     previous: Optional[dict] = None, review_request: Optional[dict] = None) -> dict:
    """전달받은 데이터셋만으로 한 번 평가해 WorkerResult를 반환한다."""
    ctx = ClinicalRun(normalize_profile(company_id, company_profile), as_of, run_id, dataset, previous, review_request)
    for step in STEPS:
        step(ctx)
    return assemble(ctx)


# ══════════════════════════════════════════════
# 그래프에 등록할 노드 함수
# ══════════════════════════════════════════════
def clinical_analysis_node(state: dict) -> dict:
    cid = state["company_id"]
    as_of = state.get("as_of") or today()
    run_id = state.get("run_id") or f"run-{as_of}"
    dataset = state.get("dataset") or {}
    previous = state.get("previous_result") or (state.get("worker_results") or {}).get(AGENT) or {}
    review = state.get("review_request") or (state.get("review_requests") or {}).get(AGENT) or {}
    try:
        result = evaluate_company(cid, state.get("company_profile") or {}, dataset, as_of, run_id, previous, review)
    except Exception as e:  # noqa: BLE001 - 실패도 결과로 반환해 누락과 구분한다
        result = S.WorkerResult(
            run_id=run_id, company_id=cid, as_of=as_of, agent=AGENT, dataset_version=dataset.get("version"),
            result_version=int(previous.get("result_version", 0)) + 1, analysis_status="failed",
            data=S.ClinicalData().model_dump(),
            errors=[S.WorkerError(code="worker_failed", message=f"{type(e).__name__}: {e}"[:300])],
        ).model_dump()
    for key in ("schema_version", "criteria_version"):  # 요청의 버전을 그대로 돌려준다
        if state.get(key):
            result[key] = state[key]
    return {"worker_results": {AGENT: result}}


# ══════════════════════════════════════════════
# 단독 실행용 결과 저장·불러오기
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
            if env.get("analysis_status") != "failed":  # 실패한 기업은 다시 평가
                out[f.stem] = env
        except Exception:
            continue
    return out


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
        d = env["data"]
        cov = {}
        for c in env["coverage"]:
            cov.setdefault(c["area"], set()).add(c["status"])
        print(f"\n=== {cid} | {env['analysis_status']} ===")
        print("  허가:", [(r["country"], r["procedure_type"], r["status"], r.get("record_number")) for r in d["regulatory_records"]])
        print("  C1:", d.get("criteria_inputs", {}).get("C1", {}).get("score"), "| 플래그:", [f["code"] for f in d["red_flags"]])
        print("  검토 상태:", {k: sorted(v) for k, v in cov.items()})
        print("  부족 정보:", [m["item"] for m in env["missing_items"]])
        if d["validation_issues"] or env["errors"]:
            print("  검증 메모:", d["validation_issues"][:3], "| 오류:", [e["code"] for e in env["errors"]])


def collect_and_evaluate(cid: str, profile: dict) -> dict:
    """단독 실행에서 데이터 로더 역할을 대신한다: 수집(데이터셋 생성) → 평가 노드"""
    try:
        from .collection import collect_clinical_data
    except ImportError:
        from collection import collect_clinical_data
    as_of = today()
    collected = collect_clinical_data(cid, profile, as_of)
    state = {"run_id": f"run-{as_of}", "company_id": cid, "as_of": as_of,
             "company_profile": collected["company_profile"], "dataset": collected["dataset"]}
    return clinical_analysis_node(state)["worker_results"][AGENT]


if __name__ == "__main__":
    # 사용: python agent.py startup_list.csv [개수] [기업명,기업명,...]
    #  - 기업별 결과를 RESULT_DIR(기본 clinical_worker_results/)에 바로 저장하고, 다시 실행하면 끝난 기업은 건너뜀
    #  - 처음부터 다시 하려면 그 폴더를 지우거나 --force
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "startup_list.csv"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    names = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
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

    todo = {cid: row for cid, row in companies.items() if cid not in done}
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=COMPANY_CONCURRENCY) as pool:
        futures = {pool.submit(collect_and_evaluate, cid, row): cid for cid, row in todo.items()}
        for fut, cid in futures.items():
            results[cid] = fut.result()
            _save_company_result(cid, results[cid])

    merged = {**done, **results}
    merged = {cid: merged[cid] for cid in companies if cid in merged}  # CSV 순서대로
    out_path = Path("clinical_worker_results.json")  # 저장 형식: {company_id: WorkerResult}
    out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
