"""
취합 데이터셋 갱신: 한 분야의 레코드를 새 수집 결과로 교체하고 기업 ID를 맞춘다.

분야 수집 코드의 출력(공통 데이터셋 {version, sources, records})을 기업별 파일로 받아,
취합본(collected-data-1)에서 그 분야의 기존 레코드를 모두 빼고 새 레코드를 기업에 확정 배정해 넣는다.

    python -m agents.data_collect.update --input collected_dataset.json --output outputs/collected_dataset.json \\
        --domain traction --datasets outputs/traction_datasets --as-of 2026-10-13 \\
        --provenance "traction_cache_2026-10-13_v2.1.zip → traction_growth.collect.collect_dataset" \\
        --rename company_d178040092e7=c042

- <datasets>/<company_id>.json : {"dataset": {...}} 또는 dataset 자체. 파일명은 취합본 ID나 분야별 원래 ID.
- 기존 분야 레코드는 확정·후보·미배정을 가리지 않고 제거한다 (다른 분야가 쓰지 않는 출처도 함께 제거).
- --rename OLD=NEW : 기업 ID를 바꾼다 (companies 키, company_id, 레코드의 company_ids).
- 입력 파일은 고치지 않고 --output에 새로 쓴다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def rename_company(collected: dict, old: str, new: str) -> None:
    companies = collected["companies"]
    if old not in companies:
        return
    if new in companies:
        raise ValueError(f"기업 ID {new}가 이미 있습니다")
    collected["companies"] = {(new if cid == old else cid): c for cid, c in companies.items()}
    company = collected["companies"][new]
    company["company_id"] = new
    if isinstance(company.get("clinical_profile"), dict) and company["clinical_profile"].get("company_id") == old:
        company["clinical_profile"]["company_id"] = new
    for r in collected["records"]:
        for key in ("company_ids", "candidate_company_ids"):
            if old in (r.get(key) or []):
                r[key] = [new if x == old else x for x in r[key]]


def replace_domain(collected: dict, domain: str, datasets: dict[str, dict], as_of: str, provenance: str) -> dict:
    """domain의 기존 레코드를 datasets({company_id: dataset})의 레코드로 교체한다. 기업별 새 레코드 수를 반환."""
    old_ids = {r["record_id"] for r in collected["records"] if r.get("domain") == domain}
    collected["records"] = [r for r in collected["records"] if r["record_id"] not in old_ids]
    for key in ("shared_record_ids", "superseded_record_ids", "unassigned_record_ids"):
        collected[key] = [i for i in collected.get(key) or [] if i not in old_ids]
    still_used = {r["source_id"] for r in collected["records"]}
    collected["sources"] = [s for s in collected["sources"] if s["source_id"] in still_used]

    known_sources = {s["source_id"]: s for s in collected["sources"]}
    known_records = {r["record_id"] for r in collected["records"]}
    counts: dict[str, int] = {}
    for cid, company in collected["companies"].items():
        for key in ("record_ids", "candidate_record_ids"):
            company[key] = [i for i in company.get(key) or [] if i not in old_ids]
        ds = datasets.get(cid) or {}
        added = []
        for s in ds.get("sources") or []:
            if s["source_id"] in known_sources:
                if known_sources[s["source_id"]] != s:
                    raise ValueError(f"{cid}: source_id 충돌 {s['source_id']}")
                continue
            collected["sources"].append(s)
            known_sources[s["source_id"]] = s
        for r in ds.get("records") or []:
            if r["record_id"] in known_records:
                raise ValueError(f"{cid}: record_id 충돌 {r['record_id']}")
            known_records.add(r["record_id"])
            tags = r.get("tags") or []
            collected["records"].append({
                **r, "tags": tags if domain in tags else [domain, *tags],
                "domain": domain, "kind": f"{domain}_collected_record",
                "company_ids": [cid], "candidate_company_ids": [], "company_link_method": "provided_company_input",
                "provenance": provenance,
                "metadata": {"input_as_of": as_of, "input_dataset_version": ds.get("version")},
            })
            added.append(r["record_id"])
        company["record_ids"] = [*company.get("record_ids", []), *added]
        company.setdefault("coverage", {})[domain] = {
            "confirmed_records": len(added), "candidate_records": 0,
            "status": "available" if added else "unmatched"}
        counts[cid] = len(added)

    collected.setdefault("domain_as_of", {})[domain] = as_of
    digest = hashlib.sha256(json.dumps([[r["record_id"], r["content"]] for r in collected["records"]],
                                       ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    previous = collected.get("dataset_version")
    collected["dataset_version"] = f"collected-{digest}"
    collected.setdefault("revisions", []).append({
        "previous_dataset_version": previous, "dataset_version": collected["dataset_version"],
        "change": f"{domain} 레코드를 새 수집 결과로 교체 (기존 {len(old_ids)}건 제거, {sum(counts.values())}건 추가)",
        "domain_as_of": as_of, "provenance": provenance})
    return counts


def load_company_datasets(folder: Path, companies: dict) -> dict[str, dict]:
    """<folder>/<ID>.json을 취합본 기업 ID 기준으로 읽는다. 분야별 원래 ID로 저장된 파일도 찾는다."""
    out = {}
    for cid, company in companies.items():
        names = dict.fromkeys([cid, *(company.get("original_company_ids") or {}).values()])
        path = next((folder / f"{n}.json" for n in names if (folder / f"{n}.json").exists()), None)
        if path:
            data = json.loads(path.read_text(encoding="utf-8"))
            out[cid] = data.get("dataset", data)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, required=True, help="취합 데이터셋 JSON")
    p.add_argument("--output", type=Path, required=True, help="새로 저장할 경로")
    p.add_argument("--domain", required=True, choices=("clinical", "market", "traction", "risk"))
    p.add_argument("--datasets", type=Path, required=True, help="기업별 새 데이터셋 폴더")
    p.add_argument("--as-of", required=True, help="새 자료의 수집 기준일 YYYY-MM-DD")
    p.add_argument("--provenance", required=True, help="새 자료의 출처 설명")
    p.add_argument("--rename", action="append", default=[], metavar="OLD=NEW", help="기업 ID 변경")
    args = p.parse_args()
    if args.output.resolve() == args.input.resolve():
        p.error("--output은 --input과 다른 경로여야 합니다")

    collected = json.loads(args.input.read_text(encoding="utf-8"))
    before = (len(collected["sources"]), len(collected["records"]))
    for pair in args.rename:
        old, new = pair.split("=", 1)
        rename_company(collected, old, new)
    counts = replace_domain(collected, args.domain, load_company_datasets(args.datasets, collected["companies"]),
                            args.as_of, args.provenance)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(collected, ensure_ascii=False), encoding="utf-8")
    print(f"출처 {before[0]} → {len(collected['sources'])} · 레코드 {before[1]} → {len(collected['records'])}")
    print(f"{args.domain}: {sum(counts.values())}건을 {sum(1 for n in counts.values() if n)}개 기업에 배정"
          f" · 자료 없는 기업 {[c for c, n in counts.items() if not n]}")
    print(f"버전 {collected['dataset_version']} → {args.output}")


if __name__ == "__main__":
    main()
