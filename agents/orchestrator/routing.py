"""Orchestrator 라우팅: 수집된 데이터셋을 보고 '자료가 있는 Worker'만 고른다.

규칙 기반(결정적)으로 판단한다. LLM을 쓰지 않는 이유:
- 같은 데이터셋이면 항상 같은 Worker가 실행돼야 재현·디버깅·비용 예측이 가능하다.
- "자료가 있는가"는 태그·필드로 확인할 수 있는 사실이라 LLM 판단이 필요 없다.

레코드가 어느 Worker 자료인지 (각 Worker 코드가 실제로 자료를 고르는 방식과 맞춤)
- clinical : tags에 "clinical" 또는 "area:*" / "tool:mfds_*" / "tool:openfda" / "tool:pubmed"
             (agents/clinical_regulatory/collection/collect.py 태그 규칙)
- market   : record["agent"] == "market" 또는 tags에 "market"   (agents/market/contract.py provided_dataset)
- traction : tags가 TRACTION_TAGS 중 하나                          (agents/traction_growth/evaluator.py select_records)
- risk     : tags에 "risk" 또는 record["agent"] == "risk"           (agents/risk/collection/prepare_input.py)
- 공통     : record["agent"] == Worker 이름이면 그 Worker 자료

다른 기업 자료(record/source의 company_id·company_ids가 다른 기업)는 세지 않는다.
어느 Worker 표시도 없는 레코드는 '미배분'으로 따로 센다.
데이터셋 전체에 배분 표시가 하나도 없으면 판단할 근거가 없으므로 4개 Worker 모두 실행한다(보수적 기본값).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

WORKERS = ("clinical", "market", "traction", "risk")
WORKER_KO = {"clinical": "임상·인허가", "market": "시장·사업성", "traction": "실적·성장성", "risk": "운영 리스크"}

# traction_growth/evaluator.py의 TRACTION_TAGS와 같은 값 (무거운 import를 피하려고 복사, 바뀌면 함께 수정)
TRACTION_TAGS = frozenset({"traction", "financial", "revenue", "disclosure", "contract", "activity", "customer",
                           "headcount", "news"})
CLINICAL_TAG_PREFIXES = ("area:", "tool:mfds", "tool:openfda", "tool:pubmed")


def _tags(record: dict) -> list[str]:
    return [t for t in record.get("tags") or [] if isinstance(t, str)]


def workers_of(record: dict) -> set[str]:
    """레코드 하나가 어느 Worker 자료인지. 표시가 없으면 빈 집합."""
    tags = _tags(record)
    agent = record.get("agent")
    out: set[str] = set()
    if agent in WORKERS:
        out.add(agent)
    if "clinical" in tags or any(t.startswith(CLINICAL_TAG_PREFIXES) for t in tags):
        out.add("clinical")
    if "market" in tags:
        out.add("market")
    if set(tags) & TRACTION_TAGS:
        out.add("traction")
    if "risk" in tags:
        out.add("risk")
    return out


def _belongs_to(item: dict, company_id: Optional[str]) -> bool:
    """다른 기업 자료면 False. 기업 표시가 없으면(산업 공통 자료 등) True."""
    if not company_id:
        return True
    cid = item.get("company_id")
    if cid not in (None, company_id):
        return False
    cids = item.get("company_ids")
    return not cids or company_id in cids


def _source_ids(record: dict) -> list[str]:
    ids = record.get("source_ids") or ([record["source_id"]] if record.get("source_id") else [])
    return [i for i in ids if isinstance(i, str)]


def plan_workers(dataset: Optional[dict], company_id: Optional[str] = None,
                 enabled: Iterable[str] = WORKERS, min_records: int = 1) -> dict[str, Any]:
    """실행 계획. 반환
    {selected: [worker], skipped: {worker: 사유}, record_counts: {worker: n}, unassigned: n, rule: str}
    """
    enabled = [w for w in WORKERS if w in set(enabled)]
    records = [r for r in (dataset or {}).get("records") or [] if isinstance(r, dict)]
    sources = {s.get("source_id"): s for s in (dataset or {}).get("sources") or [] if isinstance(s, dict)}
    counts = {w: 0 for w in enabled}
    unassigned, any_marked = 0, False
    own = []
    for r in records:
        if not _belongs_to(r, company_id):
            continue
        srcs = [sources.get(i) for i in _source_ids(r)]
        if srcs and all(s is not None and not _belongs_to(s, company_id) for s in srcs):
            continue  # 출처가 모두 다른 기업 자료
        own.append(r)
    records = own
    for r in records:
        owners = workers_of(r)
        any_marked = any_marked or bool(owners)
        if not owners:
            unassigned += 1
        for w in owners & set(enabled):
            counts[w] += 1

    if records and not any_marked:
        rule = "no_routing_marks→all"
        selected = list(enabled)
        skipped: dict[str, str] = {}
    else:
        rule = f"records>={min_records}"
        selected = [w for w in enabled if counts[w] >= min_records]
        reason = "이 기업 자료가 데이터셋에 없음" if not records else "데이터셋에 {ko} 자료 없음"
        skipped = {w: reason.format(ko=WORKER_KO[w]) for w in enabled if w not in selected}
    for w in WORKERS:
        if w not in enabled:
            skipped[w] = "실행 대상에서 제외됨(설정)"
    return {"selected": selected, "skipped": skipped, "record_counts": counts,
            "unassigned": unassigned, "rule": rule}


def slice_dataset(dataset: dict, worker: str, company_id: Optional[str] = None,
                  include_unassigned: bool = False) -> dict:
    """Worker에게 보낼 데이터셋: 그 Worker 자료 레코드 + 레코드가 참조하는 출처만. version은 그대로 둔다.

    version을 바꾸지 않는 이유: 재검토 때 Worker가 dataset_version 일치를 확인하기 때문(risk·traction).
    배분 표시가 하나도 없는 데이터셋은 자를 근거가 없으므로 그대로 보낸다.
    """
    records = [r for r in dataset.get("records") or [] if isinstance(r, dict) and _belongs_to(r, company_id)]
    if records and not any(workers_of(r) for r in records):
        return dataset
    keep = [r for r in records if worker in workers_of(r) or (include_unassigned and not workers_of(r))]
    used = {i for r in keep for i in _source_ids(r)}
    return {**dataset, "records": keep,
            "sources": [s for s in dataset.get("sources") or [] if s.get("source_id") in used]}


def merge_datasets(datasets: Iterable[dict], version: Optional[str] = None,
                   default_tags: Optional[dict[int, list[str]]] = None) -> dict:
    """Worker별 수집 결과(데이터셋 여러 개)를 하나로 합친다.

    - source_id·record_id 중복은 첫 번째를 남긴다 (같은 ID에 다른 내용이면 ValueError).
    - default_tags={i: [태그]}: i번째 데이터셋의 레코드에 배분 표시가 없으면 이 태그를 붙인다.
    - version을 안 주면 합친 내용의 해시로 만든다.
    """
    import hashlib
    import json

    sources: dict[str, dict] = {}
    records: dict[str, dict] = {}
    for i, ds in enumerate(datasets):
        for s in ds.get("sources") or []:
            sid = s["source_id"]
            if sid in sources and sources[sid] != s:
                raise ValueError(f"source_id 충돌: {sid}")
            sources.setdefault(sid, s)
        for r in ds.get("records") or []:
            rec = dict(r)
            if default_tags and i in default_tags and not workers_of(rec):
                rec["tags"] = list(dict.fromkeys(_tags(rec) + default_tags[i]))
            rid = rec["record_id"]
            if rid in records and records[rid] != rec:
                raise ValueError(f"record_id 충돌: {rid}")
            records.setdefault(rid, rec)
    body = {"sources": list(sources.values()), "records": list(records.values())}
    if not version:
        digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        version = f"merged-{digest[:12]}"
    return {"version": version, **body}
