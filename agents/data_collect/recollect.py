"""
데이터 재수집 (플로우차트 C): Orchestrator가 "데이터 부족"으로 판단했을 때 추가 자료를 찾아 데이터셋에 덧붙인다.

Orchestrator가 collection_request를 만들어 이 노드로 보내고, 이 노드는 dataset만 갱신해 돌려준다.
기존 Worker 결과의 무효화·재평가·재수집 횟수 관리는 Orchestrator가 한다.

    from agents.data_collect.recollect import data_recollect_node
    graph.add_node("collect", data_recollect_node)        # quality_check → collect → orchestrator

읽는 State (공통 계약)
- company_id, company_profile, as_of, dataset
- collection_request:
    {"request_id": "collect-01", "attempt": 1, "based_on_dataset_version": "<dataset.version>",
     "target_workers": ["traction"],
     "items": [{"worker": "traction", "missing_id": "c002:traction:mi01",
                "question": "최근 매출이나 공급 계약을 확인할 자료가 있는가?"}]}
  items[].query 를 주면 question 대신 그 문자열로 검색한다.

돌려주는 값
- dataset           : 새 자료가 있으면 레코드·출처를 덧붙이고 version을 바꾼다. 없으면 받은 그대로.
- collection_result : {request_id, attempt, status, based_on_dataset_version, dataset_version,
                       added_records, updated_workers,
                       items[{worker, missing_id, query, status, record_ids, hits, skipped{사유: 건수}}]}
    status = updated      새 자료를 추가함 → Orchestrator가 updated_workers(또는 전체)를 재평가
             no_new_data  새 자료 없음 (version 유지) → 같은 평가를 반복하지 말고 미해결로 종료
             stale_request  요청의 based_on_dataset_version이 현재 dataset.version과 다름 (수집하지 않음)
             failed       검색이 모두 실패

수집 방식
- 항목마다 "<기업명> <question>"으로 웹 검색(Tavily)한다. 기준일(as_of) 이후 자료와 기업명이 본문에 없는 자료는 버린다.
- 이미 그 Worker가 가진 출처(URL)는 다시 넣지 않는다.
- 새 레코드의 tags: [<worker>, "recollected", ...] — Orchestrator와 Worker가 tags로 배분한다.
- 식약처·DART 같은 전용 API 재조회는 하지 않는다 (질문 기반 웹 검색만).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ("clinical", "market", "traction", "risk")
MAX_SEARCH_CALLS = 6        # 요청 1건당 검색 횟수 상한
RESULTS_PER_QUERY = 4
MAX_CONTENT_CHARS = 6000
EXTRA_TAGS = {"clinical": ["tool:web"], "traction": ["news"], "market": [], "risk": []}

SearchFn = Callable[[str, str], list[dict]]   # (query, as_of) → [{title, url, content, published_at}]


def published_date(raw: Optional[str], url: str = "") -> Optional[str]:
    """검색 결과의 발행일을 YYYY-MM-DD로. 없으면 URL에 들어 있는 날짜를 쓴다. 둘 다 없으면 None."""
    if raw:
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            try:
                return parsedate_to_datetime(raw).date().isoformat()   # 'Tue, 31 May 2022 05:17:13 GMT'
            except (TypeError, ValueError):
                pass
    from agents.clinical_regulatory.utils import date_from_url
    return date_from_url(url)


def tavily_search(query: str, as_of: str) -> list[dict]:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY가 없습니다")
    response = requests.post(
        "https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"}, timeout=30,
        # end_date 필터는 날짜 없는 결과까지 빼 버려 결과가 0건이 되곤 한다 → 기준일 이후 자료는 아래에서 직접 거른다
        json={"query": query, "max_results": RESULTS_PER_QUERY, "include_raw_content": True,
              "include_published_date": True})
    response.raise_for_status()
    return [{"title": r.get("title"), "url": r.get("url"),
             "published_at": published_date(r.get("published_date"), r.get("url") or ""),
             "content": r.get("raw_content") or r.get("content") or ""} for r in response.json().get("results", [])]


def _names(profile: dict) -> list[str]:
    raw = [profile.get(k) for k in ("company_name", "display_name", "legal_name", "english_name", "name", "기업명")]
    raw += list(profile.get("aliases") or [])
    return list(dict.fromkeys(n.strip() for n in raw if isinstance(n, str) and len(n.strip()) >= 2))


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _hash(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def collect_additional(company_id: str, company_profile: dict, dataset: dict, request: dict, as_of: str,
                       search: Optional[SearchFn] = None, max_calls: int = MAX_SEARCH_CALLS) -> dict:
    """collection_request의 항목마다 추가 자료를 찾아 dataset에 덧붙인다. 입력 dataset은 고치지 않는다."""
    search = search or tavily_search
    version = dataset.get("version")
    result = {"request_id": request.get("request_id"), "attempt": request.get("attempt"),
              "based_on_dataset_version": request.get("based_on_dataset_version"), "dataset_version": version,
              "added_records": 0, "updated_workers": [], "items": []}
    if request.get("based_on_dataset_version") not in (None, version):
        return {"dataset": dataset, "collection_result": {**result, "status": "stale_request"}}

    names = _names(company_profile)
    sources = {s["source_id"]: s for s in dataset.get("sources") or []}
    source_of_url = {s["url"]: s["source_id"] for s in sources.values() if s.get("url")}
    records = list(dataset.get("records") or [])
    seen = {(w, sources.get(r.get("source_id"), {}).get("url")) for r in records for w in r.get("tags") or []}
    new_sources: list[dict] = []
    new_records: dict[tuple, dict] = {}
    searched: dict[str, list[dict]] = {}   # 같은 검색어는 한 번만 호출
    calls = 0

    for item in request.get("items") or []:
        worker = item.get("worker")
        query = item.get("query") or f"{names[0] if names else ''} {item.get('question') or ''}".strip()
        entry = {"worker": worker, "missing_id": item.get("missing_id"), "query": query, "record_ids": [],
                 "hits": 0, "skipped": {}}
        result["items"].append(entry)
        if worker not in WORKERS or not query:
            entry["status"] = "invalid_item"
            continue
        if query not in searched:
            if calls >= max_calls:
                entry["status"] = "budget_exhausted"
                continue
            calls += 1
            try:
                searched[query] = search(query, as_of)
            except Exception as e:  # noqa: BLE001 - 검색 실패도 결과로 남기고 다음 항목을 계속한다
                entry.update(status="search_failed", error=type(e).__name__)
                continue
        hits = searched[query]
        entry["hits"] = len(hits)

        def skip(reason: str) -> None:
            entry["skipped"][reason] = entry["skipped"].get(reason, 0) + 1

        for hit in hits:
            url, content = hit.get("url") or "", (hit.get("content") or "").strip()[:MAX_CONTENT_CHARS]
            published = hit.get("published_at")
            if not url.startswith(("http://", "https://")) or not content:
                skip("empty")
                continue
            if published and published > as_of:
                skip("after_as_of")   # 기준일 이후 자료
                continue
            body = _norm(f"{hit.get('title') or ''} {content}")
            if names and not any(_norm(n) in body for n in names):
                skip("company_not_mentioned")   # 이 기업을 언급하지 않는 자료
                continue
            if (worker, url) in seen:
                skip("already_in_dataset")   # 이 Worker가 이미 가진 출처
                continue
            seen.add((worker, url))
            sid = source_of_url.get(url)
            if sid is None:
                sid = source_of_url[url] = f"{company_id}:recollect:src:{_hash(url)}"
                new_sources.append({"source_id": sid, "title": hit.get("title") or url, "url": url,
                                    "published_at": published, "collected_at": as_of})
            record = new_records.get((url, content))
            if record is None:   # 같은 자료를 여러 Worker가 요청하면 레코드 하나에 tags를 합친다
                record = new_records[(url, content)] = {
                    "record_id": f"{company_id}:recollect:rec:{_hash(url, content)}", "source_id": sid,
                    "content": f"{hit.get('title') or ''}\n{content}".strip(), "locator": None,
                    "tags": [], "company_ids": [company_id],
                    "metadata": {"collection_request_id": request.get("request_id"), "missing_ids": [], "query": query}}
            record["tags"] = list(dict.fromkeys([*record["tags"], worker, "recollected", *EXTRA_TAGS[worker]]))
            if item.get("missing_id"):
                record["metadata"]["missing_ids"] = list(dict.fromkeys([*record["metadata"]["missing_ids"], item["missing_id"]]))
            entry["record_ids"].append(record["record_id"])
        entry["status"] = "added" if entry["record_ids"] else "no_new_data"

    added = list(new_records.values())
    statuses = {e["status"] for e in result["items"]}
    if not added:
        status = "failed" if statuses and statuses <= {"search_failed"} else "no_new_data"
        return {"dataset": dataset, "collection_result": {**result, "status": status}}
    new_version = f"{version}+r{request.get('attempt') or 1}-{_hash(*sorted(r['record_id'] for r in added))[:6]}"
    updated = {**dataset, "version": new_version,
               "sources": [*(dataset.get("sources") or []), *new_sources], "records": [*records, *added]}
    result.update(status="updated", dataset_version=new_version, added_records=len(added),
                  updated_workers=[w for w in WORKERS if any(w in r["tags"] for r in added)])
    return {"dataset": updated, "collection_result": result}


def _question(item: Optional[str], reason: Optional[str]) -> str:
    """부족 항목 → 검색어로 쓸 문구. item이 코드형 이름(external_dependency 등)이면 사유 문장을 쓴다."""
    item, reason = item or "", reason or ""
    return item if re.search(r"[가-힣]", item) else (reason or item)


def build_collection_request(company_id: str, worker_results: dict, dataset_version: Optional[str],
                             attempt: int = 1, required_only: bool = True) -> Optional[dict]:
    """Worker 결과의 missing_items로 collection_request를 만든다 (Orchestrator가 직접 만들지 않을 때 쓰는 보조 함수).
    missing_items 문구를 그대로 검색어로 쓰므로 검색 품질이 낮을 수 있다. 가능하면 Orchestrator가 질문을 다듬어 준다.
    요청할 항목이 없으면 None."""
    items = []
    for worker, res in (worker_results or {}).items():
        for m in (res or {}).get("missing_items") or []:
            if required_only and not m.get("required_for_assessment"):
                continue
            items.append({"worker": worker, "missing_id": m.get("missing_id"),
                          "question": _question(m.get("item"), m.get("reason"))})
    if not items:
        return None
    return {"request_id": f"{company_id}:collect:{attempt}", "attempt": attempt,
            "based_on_dataset_version": dataset_version,
            "target_workers": list(dict.fromkeys(i["worker"] for i in items)), "items": items}


def requests_from_unresolved(unresolved_items: list[dict], dataset_versions: dict[str, Optional[str]],
                             attempt: int = 1) -> dict[str, dict]:
    """보고서 취합 노드의 aggregated_result.unresolved_items → {company_id: collection_request}.

    unresolved_items (agents/investment_review/report/nodes.py aggregate_results)
    - {"kind": "data_limitation", "company_id", "criterion", "workers": [...], "text", "question"}
    - {"kind": "missing_item",    "company_id", "worker", "missing_id", "item", "reason"}
    dataset_versions: {company_id: 그 기업의 현재 dataset.version}. 여기에 없는 기업의 항목은 건너뛴다.
    """
    items: dict[str, list[dict]] = {}
    for u in unresolved_items or []:
        cid = u.get("company_id")
        if cid not in dataset_versions:
            continue
        if u.get("kind") == "data_limitation":
            question = u.get("question") or u.get("text") or ""
            new = [{"worker": w, "missing_id": f"{cid}:{u.get('criterion')}", "question": question}
                   for w in u.get("workers") or []]
        else:
            new = [{"worker": u.get("worker"), "missing_id": u.get("missing_id"),
                    "question": _question(u.get("item"), u.get("reason"))}]
        for item in new:
            if item["worker"] in WORKERS and item["question"] and item not in items.setdefault(cid, []):
                items[cid].append(item)
    return {cid: {"request_id": f"{cid}:collect:{attempt}", "attempt": attempt,
                  "based_on_dataset_version": dataset_versions[cid],
                  "target_workers": list(dict.fromkeys(i["worker"] for i in found)), "items": found}
            for cid, found in items.items() if found}


def data_recollect_node(state: dict) -> dict:
    """그래프에 등록할 노드. collection_request가 없으면 아무것도 바꾸지 않는다."""
    request = state.get("collection_request")
    if not request:
        return {}
    return collect_additional(state["company_id"], state.get("company_profile") or {}, state.get("dataset") or {},
                              request, state["as_of"])


def main() -> None:
    p = argparse.ArgumentParser(description="기업별 입력 파일에 재수집 요청을 적용해 본다 (단독 확인용).")
    p.add_argument("--bundle", type=Path, required=True, help="기업별 입력 JSON {company_id, as_of, company_profile, dataset}")
    p.add_argument("--request", type=Path, required=True, help="collection_request JSON")
    p.add_argument("--out", type=Path, required=True, help="갱신된 입력을 저장할 경로")
    args = p.parse_args()
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    out = collect_additional(bundle["company_id"], bundle["company_profile"], bundle["dataset"],
                             json.loads(args.request.read_text(encoding="utf-8")), bundle["as_of"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({**bundle, "dataset": out["dataset"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out["collection_result"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
