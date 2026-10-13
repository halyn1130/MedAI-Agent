"""Orchestrator에 넘길 기업별 입력을 준비한다 (프로젝트 루트에서 실행).

    python -m agents.data_collect
    python -m agents.orchestrator --input outputs/bundles

- 취합 데이터셋(--collected, 기본 outputs/collected_dataset.json)이 있으면 그 파일에서 기업별 입력을 꺼낸다.
  외부 조회를 하지 않는다.
- 취합 데이터셋이 없으면 기업 목록 CSV(--input, 기본 data/startup_list.csv)를 읽어 새로 수집한다.
  --recollect 를 주면 취합 데이터셋이 있어도 CSV에서 새로 수집한다.

저장: <out>/<company_id>.json = {company_id, as_of, company_profile, dataset}
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from .collect import ROOT, WORKERS, collect_company_dataset
from .load import company_bundle, load_collected


def _write(out: Path, bundle: dict) -> None:
    (out / f"{bundle['company_id']}.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _counts(bundle: dict) -> dict:
    return {w: sum(w in (r.get("tags") or []) for r in bundle["dataset"]["records"]) for w in WORKERS}


def from_collected(args: argparse.Namespace, wanted: set[str] | None) -> None:
    """취합 데이터셋 → 기업별 입력 (외부 조회 없음)"""
    collected = load_collected(args.collected)
    ids = [cid for cid, c in collected["companies"].items() if not wanted or c.get("company_name") in wanted]
    for cid in ids[:args.limit]:
        bundle = company_bundle(collected, cid)
        _write(args.out, bundle)
        print(f"{cid} {collected['companies'][cid].get('company_name')}: {_counts(bundle)}")
    print(f"취합 데이터셋 사용: {args.collected} ({collected.get('dataset_version')}) → {args.out}")


def from_csv(args: argparse.Namespace, wanted: set[str] | None) -> None:
    """기업 목록 CSV → 새로 수집 → 기업별 입력"""
    from agents.clinical_regulatory.utils import load_companies_from_csv

    workers = tuple(w.strip() for w in args.only.split(",") if w.strip())
    companies = load_companies_from_csv(str(args.input))   # company_id는 CSV 순서대로 c001, c002, …
    if wanted:
        companies = {cid: row for cid, row in companies.items() if row.get("기업명") in wanted}
    companies = dict(list(companies.items())[:args.limit])
    if not args.force:   # 이미 저장된 기업은 다시 수집하지 않는다
        companies = {cid: row for cid, row in companies.items() if not (args.out / f"{cid}.json").exists()}
    print(f"취합 데이터셋 없음 → {args.input} 의 {len(companies)}개 기업을 새로 수집")

    def run(cid: str) -> dict:
        bundle = collect_company_dataset(cid, companies[cid], args.as_of, workers)
        _write(args.out, bundle)
        return bundle

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run, cid): cid for cid in companies}
        for fut, cid in futures.items():
            bundle = fut.result()
            failed = {w: v["error"][:60] for w, v in bundle["collection_log"].items() if v["status"] == "failed"}
            print(f"{cid} {companies[cid].get('기업명')}: {_counts(bundle)}" + (f" · 실패 {failed}" if failed else ""))
    print(f"저장: {args.out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collected", type=Path, default=ROOT / "outputs" / "collected_dataset.json", help="취합 데이터셋 JSON")
    p.add_argument("--input", type=Path, default=ROOT / "data" / "startup_list.csv", help="기업 목록 CSV (기업명 열 필수)")
    p.add_argument("--out", type=Path, default=ROOT / "outputs" / "bundles", help="기업별 입력 저장 폴더")
    p.add_argument("--limit", type=int, help="앞에서부터 N개 기업만")
    p.add_argument("--names", help="기업명을 쉼표로 구분해 지정")
    p.add_argument("--recollect", action="store_true", help="취합 데이터셋이 있어도 CSV에서 새로 수집")
    p.add_argument("--as-of", default=date.today().isoformat(), help="[수집] 평가 기준일 YYYY-MM-DD")
    p.add_argument("--only", default=",".join(WORKERS), help=f"[수집] 수집할 분야 (기본: {','.join(WORKERS)})")
    p.add_argument("--concurrency", type=int, default=2, help="[수집] 동시에 수집할 기업 수 (검색 API 속도 제한 주의)")
    p.add_argument("--force", action="store_true", help="[수집] 이미 저장된 기업도 다시 수집")
    args = p.parse_args()
    unknown = {w.strip() for w in args.only.split(",") if w.strip()} - set(WORKERS)
    if unknown:
        p.error(f"알 수 없는 분야: {sorted(unknown)}")
    wanted = set(args.names.split(",")) if args.names else None
    args.out.mkdir(parents=True, exist_ok=True)
    if args.collected.exists() and not args.recollect:
        from_collected(args, wanted)
    else:
        from_csv(args, wanted)


if __name__ == "__main__":
    main()
