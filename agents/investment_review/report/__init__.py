"""최종 보고서: 구조화(context_builder) → 서술(writer, 선택) → 검증(checker) → 렌더링(renderer).

    python -m agents.investment_review.report --run-dir outputs/investment_review [--llm]
"""
from __future__ import annotations

from typing import Any

from .checker import check_narrative, check_report
from .context_builder import build_context
from .renderer import render
from .writer import write


def generate_report(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
                    llm: Any = None) -> tuple[str, dict]:
    """(Markdown, 검증 결과). llm=None이면 템플릿 문장만, "auto"면 환경변수 모델 사용."""
    ctx = build_context(reviews, analyses, companies, meta)
    narrative, notes = None, []
    if llm is not None:
        raw = write(ctx, llm)
        if raw is None:
            notes.append("LLM 서술 없음(모델 미설정 또는 호출 실패) → 템플릿 문장")
        else:
            narrative, notes = check_narrative(raw, ctx)
    markdown = render(ctx, narrative)
    check = check_report(markdown, ctx)
    check["narrative"] = {"used": sorted(narrative or {}), "notes": notes}
    return markdown, check
