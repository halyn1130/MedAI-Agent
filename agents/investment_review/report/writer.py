"""context를 받아 LLM으로 SUMMARY와 선정 기업별 핵심 검토 논점을 쓴다. 원문 검색·점수 변경 없음.

모델: REPORT_MODEL (없으면 MARKET_MODEL, OPENAI_API_KEY만 있으면 gpt-4o-mini). 모델이 없으면 None → 템플릿 문장.
출력은 checker.check_narrative로 숫자를 검증한 뒤에만 쓴다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

PROMPT = (Path(__file__).parent / "prompts/writer.md").read_text(encoding="utf-8")
DEFAULT_MODEL = "gpt-4o-mini"


class CompanyPoint(BaseModel):
    company_id: str
    point: str = Field(description="핵심 검토 논점 2~3문장")


class Narrative(BaseModel):
    summary: str = Field(description="개조식 전체 요약 (Markdown 목록)")
    points: list[CompanyPoint] = Field(default_factory=list)


def get_llm() -> Optional[Any]:
    model = os.environ.get("REPORT_MODEL") or os.environ.get("MARKET_MODEL")
    if not model and os.environ.get("OPENAI_API_KEY"):
        model = DEFAULT_MODEL
    if not model:
        return None
    from langchain.chat_models import init_chat_model
    return init_chat_model(model if ":" in model else f"openai:{model}")


def llm_input(ctx: dict) -> dict:
    """URL·긴 근거는 빼고 서술에 필요한 값만."""
    companies = []
    for c in ctx["selected"]:
        companies.append({
            "company_id": c["company_id"], "name": c["name"], "rank": c["rank"], "total": c["total"],
            "category": c["category"], "product": c["product"], "zero_filled": c["zero_filled"],
            "scores": {r["id"]: r["score"] for r in c["criteria"]},
            "market": {k: c["market"].get(k) for k in ("target_customer", "core_problem", "business_model")},
            "regulatory": [f"{r['country']} {r['authority']} {r['procedure']} {r['status']}" for r in c["clinical"]["regulatory"]],
            "studies": [s.get("design") for s in c["clinical"]["studies"]],
            "traction": {"stage": c["traction"]["stage"], "revenue_cagr": c["traction"]["revenue_cagr"],
                         "revenue": [(r["year"], r["value"]) for r in c["traction"]["revenue"]],
                         "red_flags": c["traction"]["red_flags"]},
            "risk": [o["statement"] for a in c["risk"]["areas"] for o in a["observations"]],
            "questions": c["questions"][:3],
        })
    return {"meta": ctx["meta"], "zero_filled_counts": ctx["zero_filled_counts"], "selected": companies}


def write(ctx: dict, llm: Any = "auto") -> Optional[dict]:
    """{"summary": str, company_id: str, ...} 또는 None(모델 없음·호출 실패)."""
    llm = get_llm() if llm == "auto" else llm
    if llm is None:
        return None
    try:
        out: Narrative = llm.with_structured_output(Narrative).invoke(
            [("system", PROMPT), ("user", json.dumps(llm_input(ctx), ensure_ascii=False))])
    except Exception:  # noqa: BLE001 - 서술 실패 시 템플릿 문장으로 보고서를 만든다
        return None
    return {"summary": out.summary, **{p.company_id: p.point for p in out.points}}
