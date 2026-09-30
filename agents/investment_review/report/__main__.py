"""저장된 실행 결과로 보고서만 다시 만든다. 02~05 Agent는 호출하지 않는다.

    python -m agents.investment_review.report --run-dir outputs/investment_review          # 템플릿 문장
    python -m agents.investment_review.report --run-dir outputs/investment_review --llm    # SUMMARY·논점 LLM 서술
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..inputs import load_companies
from ..policy import DEFAULT_K
from . import generate_report


def load_run(run_dir: Path) -> tuple[list[dict], dict[str, dict], dict]:
    reviews = json.loads((run_dir / "final_reviews.json").read_text(encoding="utf-8"))
    analyses: dict[str, dict] = {}
    for key in ("clinical_analysis", "market_analysis", "traction_analysis"):
        path = run_dir / f"{key}.json"
        if path.exists():
            for cid, env in json.loads(path.read_text(encoding="utf-8")).items():
                analyses.setdefault(cid, {})[key] = env
    path = run_dir / "risk_analysis.json"
    if path.exists():
        for cid, out in json.loads(path.read_text(encoding="utf-8")).items():
            analyses.setdefault(cid, {}).update(out)
    meta_path = run_dir / "run_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return reviews, analyses, meta


def write_report(run_dir: Path, reviews, analyses, companies: dict, meta: dict, llm=None) -> dict:
    markdown, check = generate_report(reviews, analyses, companies, meta, llm)
    (run_dir / "final_report.md").write_text(markdown, encoding="utf-8")
    (run_dir / "report_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8")
    return check


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--llm", action="store_true", help="SUMMARY·핵심 검토 논점을 LLM으로 서술 (숫자 검증 후 사용)")
    ap.add_argument("--as-of", help="run_meta.json이 없을 때 기준일")
    ap.add_argument("--policy", default="zero_fill")
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    args = ap.parse_args()

    reviews, analyses, meta = load_run(args.run_dir)
    as_of = meta.get("as_of") or args.as_of or (reviews[0]["as_of"] if reviews else "")
    meta = {"as_of": as_of, "criteria_version": reviews[0]["criteria_version"] if reviews else "",
            "policy": args.policy, "k": args.k, **meta}
    companies = {c["company_id"]: c for c in load_companies()}
    check = write_report(args.run_dir, reviews, analyses, companies, meta, "auto" if args.llm else None)
    print(json.dumps(check, ensure_ascii=False, indent=2))
    print(f"\n보고서: {args.run_dir / 'final_report.md'}")


if __name__ == "__main__":
    main()
