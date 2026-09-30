"""06 전체 실행: 기업 목록 → 02~05 → 검증·보완 1회 → 판정 → 선정.

    python -m agents.investment_review --out outputs/review            # 저장된 임상·Risk 결과 재사용
    python -m agents.investment_review --limit 3 --market-json m.json --traction-json t.json

저장된 결과가 있는 Agent는 다시 실행하지 않고, 06이 보완을 요청할 때만 호출한다.
결과가 없는 Agent는 처음부터 실행하므로 API 비용이 든다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import date
from pathlib import Path

from .inputs import ROOT, DEFAULT_CSV, load_companies, saved_clinical, saved_json, saved_risk
from .nodes import build_graph
from .policy import PARTIAL_POLICY, STRICT_POLICY, ZERO_FILL_POLICY

POLICIES = {"zero_fill": ZERO_FILL_POLICY, "partial": PARTIAL_POLICY, "strict": STRICT_POLICY}


def _latest_risk_run() -> Path | None:
    runs = sorted(glob.glob(str(ROOT / "agents/risk/data/full_*")))
    return Path(runs[-1]) if runs else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--out", type=Path, default=ROOT / "outputs/investment_review")
    ap.add_argument("--limit", type=int, help="앞에서부터 N개 기업만")
    ap.add_argument("--only", nargs="*", help="company_id 목록 (예: c001 c022)")
    ap.add_argument("--k", type=int, help="선정 기업 수 (기본 5)")
    ap.add_argument("--policy", choices=POLICIES, default="zero_fill")
    ap.add_argument("--clinical-dir", type=Path, default=ROOT / "agents/clinical_regulatory/clinical_results",
                    help="저장된 임상 결과 폴더 (없으면 임상 Agent 실행)")
    ap.add_argument("--risk-run-dir", type=Path, default=_latest_risk_run(), help="저장된 Risk 결과 폴더")
    ap.add_argument("--market-json", type=Path, help="저장된 시장 결과 {company_id: Envelope}")
    ap.add_argument("--traction-json", type=Path, help="저장된 실적 결과 {company_id: Envelope}")
    ap.add_argument("--max-concurrency", type=int, default=5)
    args = ap.parse_args()

    companies = load_companies(args.csv)
    if args.only:
        companies = [c for c in companies if c["company_id"] in set(args.only)]
    if args.limit:
        companies = companies[: args.limit]

    preloaded: dict[str, dict] = {c["company_id"]: {} for c in companies}
    sources = [("clinical_analysis", saved_clinical(args.clinical_dir) if args.clinical_dir and args.clinical_dir.exists() else {}),
               ("market_analysis", saved_json(args.market_json) if args.market_json else {}),
               ("traction_analysis", saved_json(args.traction_json) if args.traction_json else {})]
    for key, saved in sources:
        for cid, env in saved.items():
            if cid in preloaded:
                preloaded[cid][key] = env
    if args.risk_run_dir and args.risk_run_dir.exists():
        for cid, out in saved_risk(args.risk_run_dir, companies).items():
            rid = next(c.get("risk_company_id") for c in companies if c["company_id"] == cid)
            preloaded[cid]["risk_analysis"] = {**out["risk_analysis"], "company_id": cid, "source_company_id": rid}
            preloaded[cid]["references"] = out["references"]
    reuse = {k: sum(k in p for p in preloaded.values()) for k in
             ("clinical_analysis", "market_analysis", "traction_analysis", "risk_analysis")}
    print(f"기업 {len(companies)}개 · 저장된 결과 재사용 {reuse}")

    # 02 임상은 보완 결과를 CLINICAL_RESULT_DIR(기본: 실행 위치의 clinical_results/)에 저장한다 → 06 출력 폴더로
    os.environ.setdefault("CLINICAL_RESULT_DIR", str(args.out / "clinical_results"))
    graph = build_graph(policy=POLICIES[args.policy], k=args.k)
    out = graph.invoke({"run_id": f"review-{args.as_of}", "as_of": args.as_of,
                        "companies": companies, "preloaded": preloaded},
                       {"max_concurrency": args.max_concurrency})

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "final_reviews.json"
    path.write_text(json.dumps(out["final_reviews"], ensure_ascii=False, indent=2), encoding="utf-8")
    # Agent별 최종 분석 결과: 다음 실행에서 --market-json 등으로 재사용
    analyses = {a["company_id"]: a for a in out.get("analyses", [])}
    for key in ("clinical_analysis", "market_analysis", "traction_analysis"):
        (args.out / f"{key}.json").write_text(json.dumps(
            {cid: a[key] for cid, a in analyses.items() if key in a}, ensure_ascii=False), encoding="utf-8")
    (args.out / "risk_analysis.json").write_text(json.dumps(
        {cid: {"risk_analysis": a["risk_analysis"], "references": a.get("references", [])}
         for cid, a in analyses.items() if "risk_analysis" in a}, ensure_ascii=False), encoding="utf-8")
    names = {c["company_id"]: c["company_name"] for c in companies}
    for r in out["final_reviews"]:
        total = "-" if r["total_score"] is None else f"{r['total_score']:.1f}"
        print(f"{r['company_id']} {names[r['company_id']][:10]:10} {r['final_status']:12} {total:>6}  {r['selection']['reason']}")
    print(f"\n결과: {path}")


if __name__ == "__main__":
    main()
