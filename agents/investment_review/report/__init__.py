"""최종 보고서: 결과 취합(aggregate_results) → 보고서 작성(write_report) → 품질 평가(review_quality).

    python -m agents.investment_review.report --run-dir outputs/investment_review [--no-llm]

노드·State는 report/nodes.py(공통 State 기준), 평가 기준은 report/evaluator.py,
단독 실행 그래프와 임시 Orchestrator는 report/standalone.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..policy import SCHEMA_VERSION
from .citations import fetch_pubmed
from .nodes import ReportConfig, evaluations_from_analyses, report_check
from .standalone import REPORT_REVISION_LIMIT, build_report_graph


def run_report(evaluations: list[dict], run_id: str, as_of: str, criteria_version: str, cfg: ReportConfig,
               revision_limit: int = REPORT_REVISION_LIMIT) -> dict:
    """기업별 EvaluationState 목록 → 보고서 그래프 최종 State (aggregated_result·report·quality_review …)."""
    return build_report_graph(cfg).invoke({
        "run_id": run_id, "as_of": as_of, "schema_version": SCHEMA_VERSION, "criteria_version": criteria_version,
        "evaluations": evaluations, "aggregated_result": None, "report": None, "quality_review": None,
        "orchestration": {"retry_counts": {"report_revision": 0}, "retry_limits": {"report_revision": revision_limit}}})


def run_saved(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
              llm: Any = None, pubmed_cache: Optional[Path] = None,
              fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed, judge: Any = None,
              revision_limit: int = REPORT_REVISION_LIMIT) -> tuple[dict, ReportConfig]:
    """기존 실행 결과(06 그래프 출력·저장 파일) → 기업별 EvaluationState로 바꿔 보고서 그래프 실행.
    reviews는 후보 목록과 보완 요청 이력에만 쓰고, 판정·선정은 취합 노드가 Worker 결과로 다시 계산한다."""
    run_id = meta.get("run_id") or f"review-{meta['as_of']}"
    evaluations = evaluations_from_analyses(analyses, companies, reviews, run_id, meta["as_of"])
    cfg = ReportConfig(policy_name=meta.get("policy") or "zero_fill", k=meta.get("k"), llm=llm, judge=judge,
                       pubmed_cache=pubmed_cache, fetch=fetch)
    out = run_report(evaluations, run_id, meta["as_of"], meta.get("criteria_version") or "", cfg, revision_limit)
    return out, cfg


def generate_report(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
                    llm: Any = None, pubmed_cache: Optional[Path] = None,
                    fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed, judge: Any = None,
                    revision_limit: int = REPORT_REVISION_LIMIT) -> tuple[str, dict]:
    """(Markdown, report_check). llm·judge: None 또는 "auto"(환경변수 모델)."""
    out, _ = run_saved(reviews, analyses, companies, meta, llm, pubmed_cache, fetch, judge, revision_limit)
    return out["report"]["content"], report_check(out)
