"""최종 보고서: 결과 취합(aggregate_results) → 보고서 작성(write_report) → 품질 평가(review_quality).

    python -m agents.investment_review.pipeline --input bundles.json          # Orchestrator + 보고서 (전체)
    python -m agents.investment_review.report --run-dir outputs/investment_review  # 저장된 결과로 보고서만

노드·State는 report/nodes.py, 평가 기준은 report/evaluator.py, 보고서 단독 그래프는 report/standalone.py,
Orchestrator와 연결한 전체 그래프는 investment_review/pipeline.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..policy import SCHEMA_VERSION
from .citations import fetch_pubmed
from .nodes import REPORT_REVISION_LIMIT, ReportConfig, company_states_from_analyses, report_check
from .standalone import build_report_graph


def run_report(company_states: dict[str, dict], final_reviews: list[dict], run_id: str, as_of: str,
               criteria_version: str, cfg: ReportConfig) -> dict:
    """Orchestrator 결과(기업별 State·판정) → 보고서 그래프 최종 State (aggregated_result·report·quality_review …)."""
    return build_report_graph(cfg).invoke({
        "run_id": run_id, "as_of": as_of, "schema_version": SCHEMA_VERSION, "criteria_version": criteria_version,
        "final_reviews": final_reviews, "company_states": company_states,
        "aggregated_result": None, "report": None, "quality_review": None})


def run_saved(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
              llm: Any = None, pubmed_cache: Optional[Path] = None,
              fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed, judge: Any = None,
              revision_limit: int = REPORT_REVISION_LIMIT) -> tuple[dict, ReportConfig]:
    """저장된 실행 결과(final_reviews + 기업별 분석) → 보고서 그래프 실행."""
    run_id = meta.get("run_id") or f"review-{meta['as_of']}"
    states = company_states_from_analyses(analyses, companies, reviews, run_id, meta["as_of"])
    cfg = ReportConfig(policy_name=meta.get("policy") or "zero_fill", k=meta.get("k"), llm=llm, judge=judge,
                       pubmed_cache=pubmed_cache, fetch=fetch, revision_limit=revision_limit)
    out = run_report(states, reviews, run_id, meta["as_of"], meta.get("criteria_version") or "", cfg)
    return out, cfg


def generate_report(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
                    llm: Any = None, pubmed_cache: Optional[Path] = None,
                    fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed, judge: Any = None,
                    revision_limit: int = REPORT_REVISION_LIMIT) -> tuple[str, dict]:
    """(Markdown, report_check). llm·judge: None 또는 "auto"(환경변수 모델)."""
    out, _ = run_saved(reviews, analyses, companies, meta, llm, pubmed_cache, fetch, judge, revision_limit)
    return out["report"]["content"], report_check(out)
