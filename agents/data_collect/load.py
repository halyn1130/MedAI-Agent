"""
취합 데이터셋 로딩 (플로우차트 A): 미리 취합해 둔 파일 → 기업별 Orchestrator 입력.

취합본(collected-data-1)은 전체 기업이 한 파일에 들어 있다. 외부 조회 없이 기업 1곳의 입력으로 꺼낸다.

    {schema_version, dataset_version, as_of, companies{company_id: {...}}, sources[], records[],
     shared_record_ids[], superseded_record_ids[], unassigned_record_ids[]}

    from agents.data_collect.load import load_collected, company_bundle
    collected = load_collected("collected_dataset.json")
    bundle = company_bundle(collected, "c001")
    # {company_id, as_of, company_profile, dataset{version, sources, records}}  → agents.orchestrator 입력

    python -m agents.data_collect.load --input collected_dataset.json --out outputs/bundles
    python -m agents.data_collect.load --input collected_dataset.json --merge outputs/traction_datasets
    python -m agents.orchestrator --input outputs/bundles --dry-run

--merge <폴더>: 취합본에 없는 분야 자료를 기업별로 덧붙인다. <폴더>/<company_id>.json 은
  {"dataset": {version, sources, records}} 또는 dataset 자체. 레코드에 Worker tags가 있어야 한다.

기업에 넣는 레코드
- companies[company_id].record_ids : 그 기업 자료로 확정된 레코드
- shared_record_ids               : 모든 기업 공통 자료 (시장)
- candidate_record_ids는 기업 일치가 확인되지 않은 후보라 기본으로 넣지 않는다 (include_candidates=True로 포함)
- superseded · unassigned 레코드는 넣지 않는다

Worker 배분 표시
- Orchestrator와 Worker는 tags로 자료를 고른다. 취합본에서 tags가 없는 레코드에는 domain 값을 tags로 붙인다.
- 임상 레코드의 tags(clinical · area:* · product:* · tool:*)는 그대로 둔다.

company_profile
- 입력 CSV 행(input_profile)에 clinical_profile(정식명·영문명·제품·회사 주장 원문)을 덮어쓴다.
  임상 Worker는 products[].claims와 product_id를 여기서 읽으므로 clinical_profile이 빠지면 안 된다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ("clinical", "market", "traction", "risk")


def load_collected(path: str | Path) -> dict:
    """취합본을 읽고 record_id · source_id 조회용 색인을 붙인다."""
    collected = json.loads(Path(path).read_text(encoding="utf-8"))
    collected["_records"] = {r["record_id"]: r for r in collected["records"]}
    collected["_sources"] = {s["source_id"]: s for s in collected["sources"]}
    return collected


def _profile(company: dict) -> dict:
    clinical = {k: v for k, v in (company.get("clinical_profile") or {}).items() if v not in (None, "", [])}
    name = company.get("company_name") or clinical.get("display_name")
    profile = {**(company.get("input_profile") or {}), **clinical,
               "company_id": company["company_id"], "company_name": name}
    profile["legal_name"] = profile.get("legal_name") or name   # risk Worker는 legal_name이 필수
    aliases = [*(company.get("identity_aliases") or []), *(clinical.get("aliases") or [])]
    profile["aliases"] = [a for a in dict.fromkeys(aliases) if a and a != name]
    return profile


def _record(record: dict) -> dict:
    tags = record.get("tags") or []
    domain = record.get("domain")
    if domain in WORKERS and not any(w in tags for w in WORKERS):
        record = {**record, "tags": [domain, *tags]}   # 배분 표시가 없으면 domain을 tags로
    return record


def company_bundle(collected: dict, company_id: str, include_candidates: bool = False) -> dict:
    company = collected["companies"][company_id]
    ids = [*company.get("record_ids", []), *collected.get("shared_record_ids", [])]
    if include_candidates:
        ids += company.get("candidate_record_ids", [])
    skip = set(collected.get("superseded_record_ids") or [])
    records = [_record(collected["_records"][i]) for i in dict.fromkeys(ids)
               if i in collected["_records"] and i not in skip]
    used = {r["source_id"] for r in records}
    return {
        "company_id": company_id,
        "as_of": collected.get("as_of"),
        "company_profile": _profile(company),
        "dataset": {"version": collected.get("dataset_version"),   # 취합본 버전을 그대로 쓴다
                    "sources": [collected["_sources"][s] for s in dict.fromkeys(r["source_id"] for r in records)
                                if s in used and s in collected["_sources"]],
                    "records": records},
    }


def merge_dataset(bundle: dict, extra: dict) -> dict:
    """기업 입력에 다른 데이터셋을 덧붙인다. 같은 ID가 다른 내용이면 오류. version에는 덧붙인 버전을 잇는다."""
    extra = extra.get("dataset", extra)
    ds = bundle["dataset"]
    for key, id_key in (("sources", "source_id"), ("records", "record_id")):
        known = {x[id_key]: x for x in ds[key]}
        for item in extra.get(key) or []:
            if item[id_key] in known:
                if known[item[id_key]] != item:
                    raise ValueError(f"{bundle['company_id']}: {id_key} 충돌 {item[id_key]}")
                continue
            ds[key].append(item)
            known[item[id_key]] = item
    if extra.get("version"):
        ds["version"] = f"{ds['version']}+{extra['version']}"
    return bundle


def load_dataset_node(state: dict, collected: Optional[dict] = None, path: Optional[str | Path] = None) -> dict:
    """그래프에 넣을 때: 취합본에서 State의 company_id에 해당하는 company_profile · dataset을 꺼낸다."""
    collected = collected or load_collected(path or ROOT / "data" / "collected_dataset.json")
    bundle = company_bundle(collected, state["company_id"])
    return {"company_profile": bundle["company_profile"], "dataset": bundle["dataset"],
            "as_of": state.get("as_of") or bundle["as_of"]}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, required=True, help="취합 데이터셋 JSON")
    p.add_argument("--out", type=Path, default=ROOT / "outputs" / "bundles", help="기업별 입력 저장 폴더")
    p.add_argument("--names", help="기업명을 쉼표로 구분해 지정")
    p.add_argument("--ids", help="company_id를 쉼표로 구분해 지정")
    p.add_argument("--include-candidates", action="store_true", help="기업 일치가 확인되지 않은 후보 레코드도 포함")
    p.add_argument("--merge", type=Path, action="append", default=[], help="기업별 추가 데이터셋 폴더 (여러 번 지정 가능)")
    args = p.parse_args()

    collected = load_collected(args.input)
    companies = collected["companies"]
    ids = list(companies)
    if args.ids:
        ids = [i for i in ids if i in set(args.ids.split(","))]
    if args.names:
        ids = [i for i in ids if companies[i].get("company_name") in set(args.names.split(","))]
    args.out.mkdir(parents=True, exist_ok=True)
    for cid in ids:
        bundle = company_bundle(collected, cid, args.include_candidates)
        # 취합본의 기업 ID가 분야별 원래 ID와 다를 수 있어 원래 ID로도 찾는다
        names = dict.fromkeys([cid, *(companies[cid].get("original_company_ids") or {}).values()])
        for folder in args.merge:
            extra = next((folder / f"{n}.json" for n in names if (folder / f"{n}.json").exists()), None)
            if extra:
                merge_dataset(bundle, json.loads(extra.read_text(encoding="utf-8")))
        (args.out / f"{cid}.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        counts = {w: sum(w in r["tags"] for r in bundle["dataset"]["records"]) for w in WORKERS}
        print(f"{cid} {companies[cid].get('company_name')}: 레코드 {len(bundle['dataset']['records'])} {counts}")
    print(f"저장: {args.out} ({len(ids)}개 기업)")


if __name__ == "__main__":
    main()
