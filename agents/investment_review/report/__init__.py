"""최종 보고서: 데이터 취합(aggregate) → 보고서 작성(write_report) → 보고서 평가(evaluate_report, 미달 시 작성으로 Loop).

    python -m agents.investment_review.report --run-dir outputs/investment_review [--no-llm]

노드와 State는 report/nodes.py, 평가 기준은 report/evaluator.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .citations import fetch_pubmed
from .nodes import MAX_REPORT_ATTEMPTS, build_report_graph, report_check


def run_report(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
               llm: Any = None, pubmed_cache: Optional[Path] = None,
               fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed, judge: Any = None,
               max_attempts: int = MAX_REPORT_ATTEMPTS, trace_id: Optional[str] = None) -> dict:
    """보고서 그래프 최종 State (report_markdown·report_context·eval_result·eval_history …)."""
    graph = build_report_graph(llm, judge, pubmed_cache, fetch)
    return graph.invoke({"final_reviews": reviews, "analyses": analyses, "companies": companies, "meta": meta,
                         "max_report_attempts": max_attempts, "trace_id": trace_id})


def generate_report(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
                    llm: Any = None, pubmed_cache: Optional[Path] = None,
                    fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed, judge: Any = None,
                    max_attempts: int = MAX_REPORT_ATTEMPTS) -> tuple[str, dict]:
    """(Markdown, 검증 결과). llm=None이면 템플릿 문장만, "auto"면 환경변수 모델 사용. judge도 같다.
    논문 서지 정보는 PubMed에서 조회하고 pubmed_cache에 저장한다 (fetch=None이면 캐시만 사용)."""
    out = run_report(reviews, analyses, companies, meta, llm, pubmed_cache, fetch, judge, max_attempts)
    return out["report_markdown"], report_check(out)
