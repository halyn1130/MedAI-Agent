"""InvestmentReview 목록 + 02~05 분석 결과 → 보고서 섹션별 사실 목록. LLM 입력은 여기서만 만든다.

- 선정 기업: 제품·시장·임상·실적·운영·평가표·실사 질문. 인용한 근거는 [n] 번호로 REFERENCE와 연결.
- 미선정 기업: 판정·총점·사유 한 줄.
- REFERENCE: 선정 기업 서술에 실제로 쓴 근거의 원문 출처만 (URL 기준 중복 제거).
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from ..contract import InvestmentReview
from ..policy import CRITERIA, CRITERION_OWNER, WEIGHTS

CRITERION_NAME = {"C1": "임상 근거", "C2": "시장 성장", "C3": "수요·도입", "C4": "수익화", "C5": "실적·성장", "C6": "운영 대비"}
STATUS_KO = {"eligible": "적격", "ineligible": "부적격", "undetermined": "판단불가"}
MAX_QUESTIONS = 4
MAX_ITEMS = 2          # 기업별 시장 수치·인허가·연구·계약 개수 (5쪽 제한)
NOT_FOUND = {"not_found", "unknown", None}


class References:
    """출처를 [n] 번호로 등록한다. 같은 URL은 한 번만."""

    def __init__(self):
        self.items: list[dict] = []
        self._by_key: dict[str, int] = {}

    def add(self, source: Optional[dict]) -> Optional[int]:
        if not source:
            return None
        key = source.get("url") or source.get("source_id")
        if key not in self._by_key:
            self.items.append({k: source.get(k) for k in ("title", "publisher", "published_at", "url")})
            self._by_key[key] = len(self.items)
        return self._by_key[key]


def _source_index(analyses: dict) -> tuple[dict, dict]:
    """evidence_id → source_ids, source_id → Source (네 분석 전체)."""
    ev, src = {}, {}
    for key in ("clinical_analysis", "market_analysis", "traction_analysis"):
        env = analyses.get(key) or {}
        for e in env.get("evidence") or []:
            ev[e["evidence_id"]] = e.get("source_ids") or []
        for s in env.get("sources") or []:
            src[s["source_id"]] = s
    for s in analyses.get("references") or []:
        src[s["source_id"]] = s
    return ev, src


def _cite(ids, ev, src, refs: References) -> list[int]:
    nums = []
    for i in ids or []:
        for sid in ev.get(i, [i]):  # 근거 ID면 출처로, 이미 출처 ID면 그대로
            n = refs.add(src.get(sid))
            if n and n not in nums:
                nums.append(n)
    return nums


def _shown(items: list[dict], limit: int, ev, src, refs) -> list[dict]:
    """보여 줄 항목만 남긴 뒤 그 항목의 근거만 출처 번호로 등록한다 (REFERENCE = 본문에서 인용한 출처)."""
    out = []
    for item in items[:limit]:
        ids = item.pop("_ids", [])
        out.append({**item, "refs": _cite(ids, ev, src, refs)})
    return out


def _market(env: dict, ev, src, refs) -> dict:
    data = env.get("data") or {}
    scope = data.get("market_scope") or {}
    metrics = [{"type": m["metric_type"], "value": m["value"], "unit": m.get("unit"),
                "geography": m.get("geography"), "segment": m.get("segment"),
                "years": f"{m.get('start_year') or m.get('base_year') or ''}-{m.get('end_year') or ''}".strip("-"),
                "_ids": m.get("evidence_ids")}
               for m in data.get("market_metrics") or []
               if m.get("metric_type") in ("market_size", "cagr") and (m.get("value") or 0) > 0]
    metrics.sort(key=lambda m: m["type"] != "cagr")
    return {k: scope.get(k) for k in ("segment", "target_customer", "buyer", "core_problem", "business_model")} | {
        "metrics": _shown(metrics, MAX_ITEMS, ev, src, refs),
        "concerns": list(data.get("business_concerns") or [])[:2]}


def _clinical(env: dict, ev, src, refs) -> dict:
    data = env.get("data") or {}
    regs, seen = [], set()
    for r in data.get("regulatory_records") or []:
        key = (r.get("country"), r.get("authority"), r.get("procedure_type"), r.get("status"), r.get("permitted_use"))
        if key not in seen and r.get("status") not in NOT_FOUND:
            seen.add(key)
            regs.append({"country": r.get("country"), "authority": r.get("authority"), "procedure": r.get("procedure_type"),
                         "status": r.get("status"), "use": r.get("permitted_use"), "_ids": r.get("evidence_ids")})
    studies = [{"design": s.get("design"), "prospective": s.get("prospective"), "multicenter": s.get("multicenter"),
                "_ids": s.get("evidence_ids")} for s in data.get("clinical_studies") or []]
    products = [p.get("name") for p in (data.get("collected_profile") or {}).get("products") or [] if p.get("name")]
    return {"products": products[:2], "regulatory": _shown(regs, MAX_ITEMS, ev, src, refs),
            "studies": _shown(studies, MAX_ITEMS, ev, src, refs),
            "red_flags": [f"{f['code']} {f.get('description', '')}" for f in data.get("red_flags") or []
                          if f.get("status") != "resolved"]}


def _traction(env: dict, ev, src, refs) -> dict:
    data = env.get("data") or {}
    revenue = [{"year": int(y), "value": v.get("value"), "currency": v.get("currency"), "_ids": v.get("evidence_ids")}
               for y, v in sorted((data.get("revenue_by_year") or {}).items()) if v.get("value") is not None]
    contracts, seen = [], set()
    for c in data.get("contract_records") or []:
        name = c.get("counterparty_name")
        key = (name or "").split("(")[0].strip()
        if not name or key in seen or c.get("contract_status") == "cancelled":
            continue
        seen.add(key)
        contracts.append({"counterparty": name, "paid": c.get("is_paid"), "_ids": c.get("evidence_ids")})
    return {"stage": data.get("commercial_stage"), "invest_stage": data.get("invest_stage"),
            "revenue": _shown(revenue, len(revenue), ev, src, refs), "revenue_cagr": data.get("revenue_cagr"),
            "growth_tier": data.get("growth_tier"), "headcount_trend": data.get("headcount_trend"),
            "contracts": _shown(contracts, MAX_ITEMS, ev, src, refs),
            "red_flags": [f"{r['code']} {r.get('reason', '')}" for r in data.get("red_flags") or []]}


def _risk(risk: dict, src, refs) -> dict:
    areas = []
    for a in risk.get("areas") or []:
        obs = sorted(a.get("observations") or [], key=lambda o: o.get("kind") != "risk_signal")[:1]  # 위험 신호 먼저
        shown = [{"statement": o["statement"], "signal": o.get("kind") == "risk_signal",
                  "refs": list(dict.fromkeys(n for c in o.get("citations") or []
                                             if (n := refs.add(src.get(c.get("source_id")) or c))))}
                 for o in obs]
        areas.append({"category": a["category"], "observations": shown, "unknowns": len(a.get("unknowns") or [])})
    return {"areas": areas}


def _criteria_rows(review: InvestmentReview) -> list[dict]:
    rows = []
    for c in review.criterion_results:
        rows.append({"id": c.criterion_id, "name": CRITERION_NAME[c.criterion_id], "weight": WEIGHTS[c.criterion_id],
                     "owner": CRITERION_OWNER[c.criterion_id], "score": c.score, "zero_filled": c.zero_filled,
                     "status": c.score_status.value, "rationale": c.rationale})
    return rows


def _dedupe(items: list[str], limit: int) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))[:limit]


def build_context(reviews: list[dict], analyses: dict[str, dict], companies: dict[str, dict], meta: dict) -> dict:
    """reviews: selection 결과(순위순). analyses: {company_id: {clinical_analysis, ...}}. companies: {company_id: 프로필}."""
    reviews_m = [InvestmentReview.model_validate(r) for r in reviews]
    refs = References()
    selected = []
    for r in (x for x in reviews_m if x.selection.selected):
        a = analyses.get(r.company_id) or {}
        ev, src = _source_index(a)
        p = companies.get(r.company_id, {})
        selected.append({
            "company_id": r.company_id, "name": p.get("company_name", r.company_id), "category": p.get("대분류"),
            "product": p.get("주요 제품/서비스"), "technology": p.get("핵심 기술"),
            "rank": r.selection.rank, "total": r.total_score, "basis": r.score_basis,
            "criteria": _criteria_rows(r),
            "market": _market(a.get("market_analysis") or {}, ev, src, refs),
            "clinical": _clinical(a.get("clinical_analysis") or {}, ev, src, refs),
            "traction": _traction(a.get("traction_analysis") or {}, ev, src, refs),
            "risk": _risk(a.get("risk_analysis") or {}, src, refs),
            "concerns": _dedupe(r.report.concerns, 4),
            "questions": _dedupe(r.report.due_diligence_questions, MAX_QUESTIONS),
            "zero_filled": [c.criterion_id for c in r.criterion_results if c.zero_filled],
            "review_requests": r.review_request_ids, "review_responses": len(r.review_response_ids),
        })

    others = [{"company_id": r.company_id, "name": companies.get(r.company_id, {}).get("company_name", r.company_id),
               "status": r.final_status.value, "total": r.total_score, "rank": r.selection.rank,
               "reasons": [c.value for c in r.reason_codes],
               "zero_filled": [c.criterion_id for c in r.criterion_results if c.zero_filled]}
              for r in reviews_m if not r.selection.selected]

    status = Counter(r.final_status.value for r in reviews_m)
    zero = Counter(c.criterion_id for r in reviews_m for c in r.criterion_results if c.zero_filled)
    requests = Counter(i.split(":")[1] for r in reviews_m for i in r.review_request_ids)
    return {
        "meta": {**meta, "candidates": len(reviews_m), "selected": len(selected),
                 "status": {k: status.get(k, 0) for k in STATUS_KO}},
        "selected": selected,
        "others": others,
        "zero_filled_counts": {c: zero.get(c, 0) for c in CRITERIA},
        "review": {"requests": dict(requests), "responses": sum(len(r.review_response_ids) for r in reviews_m)},
        "references": refs.items,
    }
