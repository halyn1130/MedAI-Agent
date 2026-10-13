"""Orchestrator 실행: 수집된 데이터셋 → 자료 있는 Worker만 실행 → 품질 검증·보완 → 판정 → 선정.

    python -m agents.orchestrator --input inputs/ --out outputs/orchestrator
    python -m agents.orchestrator --input bundle.json --dry-run          # Worker 실행 없이 분기 계획만
    python -m agents.orchestrator --graph                               # 그래프 구조(Mermaid) 출력

입력 (JSON 파일 또는 폴더 안의 *.json, 파일 하나에 기업 1곳 · 목록 · {company_id: 기업} 모두 가능)
    {"company_id": "c001", "company_profile": {...},
     "dataset": {"version": "...", "sources": [...], "records": [...]}}
    - dataset 대신 "datasets": [ds1, ds2, ...] 를 주면 하나로 합친다 (Worker별 수집 결과를 그대로 넣을 때).
    - 선택: run_id, as_of, schema_version, criteria_version (없으면 명령행 값·기본값)
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .routing import WORKER_KO, WORKERS, merge_datasets, plan_workers


def load_bundles(paths: list[Path]) -> list[dict]:
    files: list[Path] = []
    for p in paths:
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    out: list[dict] = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "company_id" not in data and all(isinstance(v, dict) for v in data.values()):
            data = [{"company_id": cid, **v} for cid, v in data.items()]
        out += data if isinstance(data, list) else [data]
    for b in out:
        if "dataset" not in b and b.get("datasets"):
            b["dataset"] = merge_datasets(b.pop("datasets"))
        b.setdefault("company_profile", {})
        b["company_profile"].setdefault("company_id", b["company_id"])
    return out


def main() -> None:
    from agents.investment_review.policy import DEFAULT_K, PARTIAL_POLICY, STRICT_POLICY, ZERO_FILL_POLICY
    policies = {"zero_fill": ZERO_FILL_POLICY, "partial": PARTIAL_POLICY, "strict": STRICT_POLICY}

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, nargs="*", default=[])
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--out", type=Path, default=Path("outputs/orchestrator"))
    ap.add_argument("--k", type=int, help=f"선정 기업 수 (기본 {DEFAULT_K})")
    ap.add_argument("--policy", choices=policies, default="zero_fill")
    ap.add_argument("--max-review-rounds", type=int, default=1, help="기업당 보완 라운드 상한")
    ap.add_argument("--workers", nargs="*", choices=WORKERS, default=list(WORKERS), help="실행 허용 Worker")
    ap.add_argument("--min-records", type=int, default=1, help="Worker 실행에 필요한 최소 레코드 수")
    ap.add_argument("--max-concurrency", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="분기 계획만 출력 (Worker 미실행, 비용 없음)")
    ap.add_argument("--graph", action="store_true", help="기업 그래프 Mermaid 출력")
    args = ap.parse_args()

    if args.graph:
        from .graph import build_company_graph
        fake = {w: (lambda s: {}) for w in WORKERS}
        print(build_company_graph(fake).get_graph().draw_mermaid())
        return

    bundles = load_bundles(args.input)
    if not bundles:
        ap.error("--input 으로 기업 입력(JSON)을 지정하세요")
    for b in bundles:
        p = plan_workers(b.get("dataset"), b["company_id"], args.workers, args.min_records)
        chosen = ", ".join(WORKER_KO[w] for w in p["selected"]) or "없음"
        print(f"{b['company_id']}: 실행 {chosen} | 레코드 {p['record_counts']} | 미배분 {p['unassigned']} | {p['rule']}")
    if args.dry_run:
        return

    from dotenv import load_dotenv
    load_dotenv()
    from .graph import build_graph
    graph = build_graph(policy=policies[args.policy], k=args.k, max_review_rounds=args.max_review_rounds,
                        enabled=args.workers, min_records=args.min_records)
    out = graph.invoke({"run_id": f"orch-{args.as_of}", "as_of": args.as_of, "companies": bundles},
                       {"max_concurrency": args.max_concurrency})

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "final_reviews.json").write_text(json.dumps(out["final_reviews"], ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    (args.out / "worker_results.json").write_text(json.dumps(
        {a["company_id"]: a for a in out.get("analyses", [])}, ensure_ascii=False), encoding="utf-8")
    for r in out["final_reviews"]:
        total = "-" if r["total_score"] is None else f"{r['total_score']:.1f}"
        o = r.get("orchestration") or {}
        ran = ",".join(o.get("plan", {}).get("selected", []))
        print(f"{r['company_id']} {r['final_status']:12} {total:>6}  실행[{ran}] 보완 {o.get('review_rounds', 0)}회"
              f"  {r['selection']['reason']}")
    print(f"\n결과: {args.out / 'final_reviews.json'}")


if __name__ == "__main__":
    main()
