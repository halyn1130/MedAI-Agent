"""최종 보고서: 구조화(context_builder) → 서술(writer, 선택) → 검증(checker) → 렌더링(renderer).

    python -m agents.investment_review.report --run-dir outputs/investment_review [--llm]
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .checker import check_narrative, check_report
from .citations import fetch_pubmed, pubmed_metadata
from .context_builder import build_context
from .renderer import render
from .writer import write


def generate_report(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict,
                    llm: Any = None, pubmed_cache: Optional[Path] = None,
                    fetch: Optional[Callable[[list[str]], dict]] = fetch_pubmed) -> tuple[str, dict]:
    """(Markdown, 검증 결과). llm=None이면 템플릿 문장만, "auto"면 환경변수 모델 사용.
    논문 서지 정보는 PubMed에서 조회하고 pubmed_cache에 저장한다 (fetch=None이면 캐시만 사용)."""
    ctx = build_context(reviews, analyses, companies, meta)
    ctx["pubmed"] = pubmed_metadata(ctx["references"], pubmed_cache, fetch or (lambda _ids: {}))
    narrative, notes = None, []
    if llm is not None:
        raw = write(ctx, llm)
        if raw is None:
            notes.append("LLM 서술 없음(모델 미설정 또는 호출 실패) → 템플릿 문장")
        else:
            narrative, notes = check_narrative(raw, ctx)
            if notes:  # 검증에 걸린 항목만 한 번 더 요청
                retry = write(ctx, llm, feedback=notes)
                fixed, retry_notes = check_narrative(retry or {}, ctx)
                failed = {n.split(":")[0] if n.startswith(("summary", "criteria_guide")) else n.split(":")[0] + ":" + n.split(":")[1]
                          for n in notes}
                narrative.update({k: v for k, v in fixed.items() if k in failed or k not in narrative})
                notes = [f"1차: {n}" for n in notes] + [f"재요청 후: {n}" for n in retry_notes
                                                        if n.split(":")[0] in {f.split(":")[0] for f in failed}]
    markdown = render(ctx, narrative)
    check = check_report(markdown, ctx)
    check["narrative"] = {"used": sorted(narrative or {}), "notes": notes}
    return markdown, check
