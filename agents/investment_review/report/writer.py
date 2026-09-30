"""context를 받아 LLM으로 보고서 문장을 쓴다 (prompts/writer.md). 원문 검색·점수 변경 없음.

LLM이 쓰는 것: 요약, 평가 기준 안내문, 선정 이유·핵심 검토 논점.
부적격·판단불가 사유는 프롬프트 4번 규칙(코드 → 문장, 0점 항목 풀어쓰기)을 context_builder.explain이 코드로 만든다
(LLM은 38개 목록을 끝까지 쓰지 못하고 항목 개수를 잘못 세서, 코드로 만들어야 전 기업이 같은 형식·정확한 값이 된다).
참고문헌은 프롬프트 5번 규칙을 구현한 citations.format_reference로 만든다(LLM은 발행연도·사이트명·URL을 자주 틀려서
출처를 입력에 넣지 않는다).
모델: REPORT_MODEL (없으면 gpt-4.1). OPENAI_API_KEY가 없으면 None → 템플릿 문장.
(gpt-4o-mini는 요약 순서 누락·항목 혼동이 잦아 기본값으로 쓰지 않는다.)
출력은 checker.check_narrative로 검증한 뒤, 통과한 문장만 쓴다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .renderer import _money

PROMPT = (Path(__file__).parent / "prompts/writer.md").read_text(encoding="utf-8")
EASY = {"C1": "제품 효과를 검증한 임상 연구", "C2": "대상 시장의 성장성", "C3": "병원·고객의 실제 도입 수요",
        "C4": "매출을 내는 구조(가격·판매 방식)", "C5": "매출·유료 계약 실적과 성장 추이",
        "C6": "외부 의존·핵심 인력·운영 사고 등 운영 위험 대비"}
DEFAULT_MODEL = "gpt-4.1"


class CompanyText(BaseModel):
    name: str
    selection_reason: str = ""
    points: str = ""


class IneligibleText(BaseModel):
    name: str
    reason_text: str


class ReferenceText(BaseModel):
    id: int
    text: str


class Narrative(BaseModel):
    summary: str = ""
    criteria_guide: str = ""
    companies: list[CompanyText] = Field(default_factory=list)
    ineligible: list[IneligibleText] = Field(default_factory=list)
    references: list[ReferenceText] = Field(default_factory=list)


def get_llm() -> Optional[Any]:
    model = os.environ.get("REPORT_MODEL")
    if not model and os.environ.get("OPENAI_API_KEY"):
        model = DEFAULT_MODEL
    if not model:
        return None
    from langchain.chat_models import init_chat_model
    return init_chat_model(model if ":" in model else f"openai:{model}")


def llm_input(ctx: dict) -> dict:
    """프롬프트가 참조하는 값(항목 번호, 판정 코드, 0점 처리 항목, 출처)만 넘긴다."""
    companies = []
    for c in ctx["selected"]:
        companies.append({
            "name": c["name"], "rank": c["rank"], "total": c["total"], "category": c["category"],
            "product": c["product"],
            "scores": {r["id"]: {"name": r["name"], "score": r["score"], "zero_filled": r["zero_filled"]}
                       for r in c["criteria"]},
            "zero_filled": c["zero_filled_ids"],
            "zero_filled_easy": [EASY[x] for x in c["zero_filled_ids"]],
            "confirmed_items": [f"{r['id']} {EASY[r['id']]} {r['score']:g}점" for r in c["criteria"]
                                if not r["zero_filled"] and r["score"] is not None],
            "facts": {
                "regulatory": [f"{r['country']} {r['authority']} {r['procedure']} {r['status']}" for r in c["clinical"]["regulatory"]],
                "studies": [s.get("design") for s in c["clinical"]["studies"]],
                "commercial_stage": c["traction"]["stage"],
                "revenue": [f"{r['year']}년 {_money(r['value'], r['currency'])}" for r in c["traction"]["revenue"]],
                "revenue_cagr": f"{c['traction']['revenue_cagr']:.1%}" if c["traction"]["revenue_cagr"] is not None else None,
                "contracts": [x["counterparty"] for x in c["traction"]["contracts"]],
                "target_customer": c["market"].get("target_customer"),
                "core_problem": c["market"].get("core_problem"),
            },
            "red_flags": c["red_flag_codes"],
            "due_diligence_questions": c["questions"][:3],
            "top_items": [f"{r['id']} {r['name']} {r['score']:g}점" for r in
                          sorted((r for r in c["criteria"] if not r["zero_filled"] and r["score"] is not None),
                                 key=lambda r: (-r["score"], -r["weight"]))[:2]],
        })
    ineligible = [{"name": o["name"], "status": o["status"], "total": o["total"], "reason_codes": o["reasons"],
                   "zero_filled": o["zero_filled_ids"], "zero_filled_count": len(o["zero_filled_ids"]),
                   "zero_filled_easy": [EASY[c] for c in o["zero_filled_ids"]],
                   "below_2_easy": [EASY[c] for c in o.get("low_criteria", [])]}
                  for o in ctx["others"] if o["status"] != "eligible"]
    meta = ctx["meta"]
    reasons = sorted((ctx.get("reason_counts") or {}).items(), key=lambda x: -x[1])
    zero = sorted((ctx.get("zero_filled_counts_by_id") or {}).items(), key=lambda x: -x[1])
    return {"as_of": meta["as_of"], "candidates": meta["candidates"], "status": meta["status"], "k": meta["k"],
            "selected_list": [f"{c['name']} {c['total']:g}점" for c in ctx["selected"]],
            "main_ineligible_reason": f"{reasons[0][0]} {reasons[0][1]}개 기업" if reasons else None,
            "reason_counts": dict(reasons),
            "zero_filled_top": [f"{c} {EASY[c]} {n}개 기업" for c, n in zero if n][:3],
            "selected": companies, "ineligible": [], "sources": []}  # 부적격 사유·참고문헌은 코드로 만든다


def write(ctx: dict, llm: Any = "auto", feedback: Optional[list[str]] = None) -> Optional[dict]:
    """{"summary", "criteria_guide", "select:<id>", "points:<id>", "reason:<id>"} 또는 None.
    feedback: 이전 출력이 검증에 걸린 이유. 주면 고쳐 쓰도록 요청한다."""
    llm = get_llm() if llm == "auto" else llm
    if llm is None:
        return None
    messages = [("system", PROMPT), ("user", json.dumps(llm_input(ctx), ensure_ascii=False))]
    if feedback:
        messages.append(("user", "이전 출력이 아래 이유로 사용되지 않았다. 규칙과 입력값을 지켜 다시 써라.\n- "
                         + "\n- ".join(feedback)))
    try:
        out: Narrative = llm.with_structured_output(Narrative).invoke(messages)
    except Exception:  # noqa: BLE001 - 서술 실패 시 템플릿 문장으로 보고서를 만든다
        return None
    ids = {c["name"]: c["company_id"] for c in ctx["selected"]} | {o["name"]: o["company_id"] for o in ctx["others"]}
    texts = {"summary": out.summary, "criteria_guide": out.criteria_guide}
    for c in out.companies:
        if c.name in ids:
            texts[f"select:{ids[c.name]}"] = c.selection_reason
            texts[f"points:{ids[c.name]}"] = c.points
    for o in out.ineligible:
        if o.name in ids:
            texts[f"reason:{ids[o.name]}"] = o.reason_text
    return {k: v for k, v in texts.items() if v}
