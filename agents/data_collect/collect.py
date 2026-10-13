"""
전체 데이터셋 수집: 분야별 수집 코드를 불러 기업 1곳의 공통 데이터셋을 만든다.

평가 Worker(clinical · market · traction · risk)는 수집하지 않는다.
이 모듈이 각 분야의 수집 함수를 호출하고 결과를 하나의 dataset으로 합친다.

    from agents.data_collect import collect_company_dataset, dataset_for_worker
    out = collect_company_dataset("c001", {"기업명": "레모넥스", ...}, "2026-10-13")
    state["company_profile"] = out["company_profile"]
    state["dataset"] = out["dataset"]                       # {version, sources, records}
    worker_dataset = dataset_for_worker(out["dataset"], "risk")   # Worker별로 잘라 전달할 때

분야별 수집 (각 담당 폴더의 코드를 그대로 import)
- clinical : agents.clinical_regulatory.collection.collect_clinical_data
             식약처·openFDA·PubMed·웹 검색. 정식명·제품·회사 주장 원문도 채운다 (LLM 사용)
- traction : agents.traction_growth.collect.collect_dataset
             OpenDART·뉴스(네이버/Tavily)·국민연금 (LLM 없음)
- risk     : agents.risk.collection.collect.collect_company + 품질 선별
             Tavily 검색 (LLM 없음). 임시 폴더에 받은 뒤 선별 통과분만 가져온다
- market   : data/market_evaluation/dataset.json
             기업별 수집이 없다. 산업 공통 자료(고정 데이터셋)를 그대로 싣는다

레코드 배분 규칙
- 모든 레코드의 tags에 Worker 이름(clinical · market · traction · risk)이 들어간다.
- risk Worker는 tags를 보지 않고 전달된 레코드를 모두 읽고, market Worker는 `agent` 값만 본다.
  그래서 Worker에 넘길 때는 dataset_for_worker()로 그 Worker의 레코드만 잘라 전달한다.
- 분야 수집 코드가 붙인 tags와 Source의 부가 필드(publisher, source_type 등)는 바꾸지 않는다.

한 분야가 실패해도 나머지는 계속 수집하고, 실패 내용은 collection_log에 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ("clinical", "market", "traction", "risk")
MARKET_DATASET = ROOT / "data" / "market_evaluation" / "dataset.json"
RISK_MAX_SEARCH_CALLS = 9

Collected = tuple[dict, list[dict], list[dict], dict]   # (프로필 보강값, sources, records, 로그)


def _company_name(profile: dict) -> str:
    for key in ("display_name", "company_name", "name", "기업명", "legal_name"):
        if profile.get(key):
            return str(profile[key]).strip()
    raise ValueError("company_profile에 기업명이 없습니다 (기업명 · company_name · legal_name)")


# ══════════════════════════════════════════════
# 분야별 수집
# ══════════════════════════════════════════════
def _collect_clinical(cid: str, profile: dict, as_of: str) -> Collected:
    from agents.clinical_regulatory.collection import collect_clinical_data

    out = collect_clinical_data(cid, profile, as_of)
    return out["company_profile"], out["dataset"]["sources"], out["dataset"]["records"], {"issues": out["issues"]}


def _collect_traction(cid: str, profile: dict, as_of: str) -> Collected:
    from agents.traction_growth.agent import make_ctx
    from agents.traction_growth.collect import collect_dataset

    ctx = make_ctx(profile, as_of=as_of, llm=None)   # 수집만: LLM 없이 클라이언트·검색 계획만 구성
    dataset, log = collect_dataset(ctx, "traction")
    return {}, dataset["sources"], dataset["records"], {
        "status": log["status"], "notes": log["notes"], "search_calls": len(log["search_log"])}


def _collect_risk(cid: str, profile: dict, as_of: str) -> Collected:
    from agents.risk.collection.collect import collect_company
    from agents.risk.collection.search import TavilySearch

    risk_profile = {"company_id": cid, "company_name": _company_name(profile)}
    with tempfile.TemporaryDirectory() as tmp:   # risk 수집기는 폴더에 저장하는 방식이라 임시 폴더를 쓴다
        folder = Path(tmp) / "company"
        manifest = collect_company(risk_profile, TavilySearch(), folder, date.fromisoformat(as_of),
                                   RISK_MAX_SEARCH_CALLS)
        state = json.loads((folder / "reviewed" / "state.json").read_text(encoding="utf-8"))
    sources, records = [], []
    for i, item in enumerate(state.get("company_evidence") or [], 1):   # 품질 선별을 통과한 자료만
        sources.append({"source_id": item["source_id"], "title": item.get("title"), "url": item.get("url"),
                        "published_at": item.get("published_at"), "collected_at": item.get("checked_at"),
                        "publisher": item.get("publisher"), "source_type": item.get("source_type")})
        records.append({"record_id": f"{cid}:risk:rec{i:03d}", "source_id": item["source_id"],
                        "content": item["content"], "locator": None, "tags": ["risk"], "company_ids": [cid]})
    review = manifest.get("quality_review") or {}
    return {}, sources, records, {
        "search_calls": manifest["search_calls"], "failed_calls": manifest["failed_calls"],
        "collected": manifest["source_count"], "accepted": review.get("candidates"),
        "coverage": {k: v["collection_status"] for k, v in (state.get("risk_collection_coverage") or {}).items()}}


def _collect_market(cid: str, profile: dict, as_of: str) -> Collected:
    """산업 공통 자료. 기업마다 같은 내용이며 외부 조회를 하지 않는다."""
    shared = json.loads(MARKET_DATASET.read_text(encoding="utf-8"))
    sources = [{**s, "collected_at": s.get("collected_at") or s.get("checked_at")} for s in shared["sources"]]
    records = []
    for r in shared["records"]:
        r = dict(r)
        if "content" not in r:   # 공통 계약의 필드명으로 맞춘다 (시장 Worker는 content를 먼저 읽는다)
            r["content"] = r.pop("text", "")
        r["tags"] = list(dict.fromkeys(["market", *(r.get("tags") or [])]))
        records.append(r)
    return {}, sources, records, {"shared_dataset_version": shared.get("version")}


COLLECTORS: dict[str, Callable[[str, dict, str], Collected]] = {
    "clinical": _collect_clinical, "market": _collect_market,
    "traction": _collect_traction, "risk": _collect_risk,
}


# ══════════════════════════════════════════════
# 병합
# ══════════════════════════════════════════════
def _merge(dataset: dict, worker: str, sources: list[dict], records: list[dict]) -> None:
    """분야 결과를 합친다. ID가 다른 분야와 겹치면 Worker 이름을 붙여 구분한다."""
    known_s, known_r = dataset["_sources"], dataset["_records"]
    renamed: dict[str, str] = {}
    for s in sources:
        sid = s["source_id"]
        if sid in known_s and known_s[sid] != s:
            renamed[sid] = f"{worker}:{sid}"
            s = {**s, "source_id": renamed[sid]}
        known_s[s["source_id"]] = s
    for r in records:
        r = {**r, "source_id": renamed.get(r["source_id"], r["source_id"])}
        if worker not in (r.get("tags") or []):
            r["tags"] = [worker, *(r.get("tags") or [])]
        if r["record_id"] in known_r:
            r["record_id"] = f"{worker}:{r['record_id']}"
        known_r[r["record_id"]] = r


def collect_company_dataset(company_id: str, company_profile: dict, as_of: Optional[str] = None,
                            workers: tuple[str, ...] = WORKERS) -> dict:
    """기업 1곳의 전체 데이터셋을 수집한다. clinical을 먼저 돌려 보강된 프로필(정식명·영문명)을 다른 분야에 넘긴다."""
    load_dotenv(ROOT / ".env")
    as_of = as_of or date.today().isoformat()
    name = _company_name(company_profile)
    profile = {**company_profile, "company_id": company_id, "company_name": name}
    merged = {"_sources": {}, "_records": {}}
    log: dict[str, dict] = {}

    for worker in sorted(workers, key=lambda w: w != "clinical"):   # clinical 먼저
        try:
            extra, sources, records, info = COLLECTORS[worker](company_id, profile, as_of)
        except Exception as e:  # noqa: BLE001 - 한 분야 실패가 전체 수집을 멈추지 않게 한다
            log[worker] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
            continue
        profile = {**profile, **{k: v for k, v in extra.items() if v not in (None, "", [])}}
        _merge(merged, worker, sources, records)
        log[worker] = {"status": "collected", "sources": len(sources), "records": len(records), "detail": info}

    profile["legal_name"] = profile.get("legal_name") or name   # risk Worker는 legal_name이 필수
    sources, records = list(merged["_sources"].values()), list(merged["_records"].values())
    digest = hashlib.sha1(json.dumps([[r["record_id"], r["content"]] for r in records],
                                     ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:10]
    return {
        "company_id": company_id, "as_of": as_of,
        "company_profile": profile,
        "dataset": {"version": f"dataset-{as_of}-{digest}", "sources": sources, "records": records},
        "collection_log": log,   # 분야별 수집 상태 (State에는 넣지 않는다)
    }


def dataset_for_worker(dataset: dict, worker: str) -> dict:
    """Worker 하나에 넘길 dataset: tags에 그 Worker 이름이 있는 레코드와 그 출처만 남긴다. version은 유지한다."""
    records = [r for r in dataset.get("records") or [] if worker in (r.get("tags") or [])]
    used = {r["source_id"] for r in records}
    return {"version": dataset.get("version"), "records": records,
            "sources": [s for s in dataset.get("sources") or [] if s["source_id"] in used]}


# ══════════════════════════════════════════════
# 그래프에 등록할 노드 (최초 데이터 로딩)
# ══════════════════════════════════════════════
def data_collect_node(state: dict) -> dict:
    """company_profile로 전체 데이터셋을 수집해 State에 넣는다.
    재수집 요청(collection_request)에 따른 추가 수집은 아직 다루지 않는다."""
    out = collect_company_dataset(state["company_id"], state["company_profile"], state.get("as_of"))
    return {"company_profile": out["company_profile"], "dataset": out["dataset"]}
