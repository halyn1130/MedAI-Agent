"""InvestmentReview 목록 + 02~05 분석 결과 → 보고서 섹션별 사실 목록. LLM 입력은 여기서만 만든다.

- 선정 기업: 제품·시장·임상·실적·운영·평가표·실사 질문. 인용한 근거는 [n] 번호로 REFERENCE와 연결.
- 미선정 기업: 판정·총점·사유 한 줄.
- REFERENCE: 선정 기업 서술에 실제로 쓴 근거의 원문 출처만 (URL 기준 중복 제거).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from ..contract import InvestmentReview
from ..policy import CRITERIA, CRITERION_OWNER, WEIGHTS

CRITERION_NAME = {"C1": "임상 근거", "C2": "시장 성장", "C3": "고객 수요·도입", "C4": "수익화", "C5": "실적·성장", "C6": "운영 대비"}
STATUS_KO = {"eligible": "적격", "ineligible": "부적격", "undetermined": "판단불가"}
MAX_QUESTIONS = 2
MAX_ITEMS = 2          # 기업별 시장 수치·인허가·연구·계약 개수 (5쪽 제한)
NOT_FOUND = {"not_found", "unknown", "not_applicable", None}


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
            self.items.append({k: source.get(k) for k in ("title", "publisher", "published_at", "url", "source_type")})
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
        "metrics": _shown(metrics, 1, ev, src, refs),
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
            "red_flags": [f.get("description", "") for f in data.get("red_flags") or []
                          if f.get("status") != "resolved"]}


def _traction(env: dict, ev, src, refs) -> dict:
    data = env.get("data") or {}
    revenue = [{"year": int(y), "value": v.get("value"), "currency": v.get("currency"), "_ids": v.get("evidence_ids")}
               for y, v in sorted((data.get("revenue_by_year") or {}).items()) if v.get("value") is not None]
    contracts, seen = [], set()
    for c in data.get("contract_records") or []:
        name = c.get("counterparty_name")
        key = (name or "").split("(")[0].strip()
        if not name or key in ("미상", "unknown", "") or key in seen or c.get("contract_status") == "cancelled":
            continue
        seen.add(key)
        contracts.append({"counterparty": name, "paid": c.get("is_paid"), "_ids": c.get("evidence_ids")})
    return {"stage": data.get("commercial_stage"), "invest_stage": data.get("invest_stage"),
            "revenue": _shown(revenue, len(revenue), ev, src, refs), "revenue_cagr": data.get("revenue_cagr"),
            "growth_tier": data.get("growth_tier"), "headcount_trend": data.get("headcount_trend"),
            "contracts": _shown(contracts, MAX_ITEMS, ev, src, refs),
            "red_flags": [r.get("reason", "") for r in data.get("red_flags") or []]}


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


LEVEL_TEXT = {"L1": "회사 자체 발표만 있어 방법·결과를 독립적으로 확인하기 어려움",
              "L2": "제품 관련 관찰·성능 연구의 방법·기관·표본·주요 지표를 확인함",
              "L3": "전향적 다기관 연구 또는 독립 외부 검증을 확인함",
              "L4": "사전 정의한 평가변수와 비교설계를 갖춘 확증 연구를 확인함"}
SCORE_LEVEL = {1.0: "L1", 2.0: "L2", 4.0: "L3", 5.0: "L4"}
CAGR_BAND = {0: "0% 미만", 1: "0~5%", 2: "5~10%", 3: "10~15%", 4: "15~20%", 5: "20% 이상"}
STAGE_TEXT = {"A": "최근 36개월 매출 확인", "B": "유료 계약 확인", "C": "실증 확인", "D": "MOU·수상만 확인"}
DIM_KO = {"demand": "고객 수요", "commercialization": "도입·상용화", "monetization": "수익화"}
AREA_TEXT = {"external_dependency": "외부 의존", "key_person_continuity": "핵심 인력", "operational_incidents": "운영 사건"}
ZERO = " → 확인하지 못해 0점 처리"


def _checks(dims: dict, name: str) -> str:
    checks = (dims.get(name) or {}).get("checks") or []
    if not checks:
        return f"{DIM_KO[name]} 체크 결과 없음"
    n = Counter(c.get("status") for c in checks)
    if n["yes"] + n["no"] == 0:
        return f"{DIM_KO[name]} 체크 {len(checks)}개 모두 확인 불가"
    return f"{DIM_KO[name]} 체크 {len(checks)}개 중 충족 {n['yes']}개" + (f"·미충족 {n['no']}개" if n["no"] else "") + \
        (f"·확인 불가 {len(checks) - n['yes'] - n['no']}개" if len(checks) - n["yes"] - n["no"] else "")


def _reason(cid: str, c, a: dict) -> str:
    """항목별 점수의 이유. 02~05 결과에 있는 값만 쓴다."""
    zero = ZERO if c.zero_filled else ""
    if cid == "C1":
        data = (a.get("clinical_analysis") or {}).get("data") or {}
        if c.zero_filled or c.score is None:
            na = (data.get("criteria_inputs") or {}).get("C1", {}).get("score_status") == "not_applicable"
            return ("임상 분석에서 평가 대상 제품이 없다고 판단함" if na else "평가 가능한 제품 임상 근거를 공개자료에서 찾지 못함") + zero
        level = SCORE_LEVEL.get(c.score)  # 설계서: 1=L1, 2=L2, 4=L3, 5=L4. 소수는 제품별 가중평균
        if not level:
            levels = sorted({p.get("evidence_level") for p in data.get("product_assessments") or []
                             if p.get("evidence_level") in LEVEL_TEXT})
            level = levels[-1] if levels else None
        text = LEVEL_TEXT[level] if level else "제품별 임상 근거 수준의 가중평균"
        return text + \
            (" · 근거를 판단할 수 없는 제품은 평가에서 제외" if "판단불가 제품 제외" in c.rationale else "")
    if cid == "C2":
        if c.score is None or c.zero_filled:
            return "시장 성장률 수치를 찾지 못함" + zero
        band = CAGR_BAND.get(int(c.score), "")
        m = re.search(r"중앙값 ([\d.]+%)", c.rationale)
        if m:
            return f"세부시장 성장률 근거가 없어 시장·사업성 분석이 찾은 상위 시장 연평균 성장률 중앙값 {m.group(1)}로 평가 ({band} 구간)"
        return f"세부시장 연평균 성장률 기준 {band} 구간"
    if cid in ("C3", "C4"):
        dims = ((a.get("market_analysis") or {}).get("data") or {}).get("dimensions") or {}
        names = ("demand", "commercialization") if cid == "C3" else ("monetization",)
        text = ", ".join(_checks(dims, n) for n in names)
        if cid == "C3" and "확인된 영역만으로" in c.rationale:
            text += " → 확인된 영역만으로 평가"
        return text + zero
    if cid == "C5":
        data = (a.get("traction_analysis") or {}).get("data") or {}
        if c.zero_filled or c.score is None:
            return "최근 36개월 인정 가능한 매출·유료 계약·실증 근거를 찾지 못함" + zero
        c5 = (data.get("criteria_inputs") or {}).get("C5") or {}
        text = f"{STAGE_TEXT.get(data.get('commercial_stage'), '상업화 단계 확인')}(상업화 {c5.get('commercial_score')}점)"
        if c5.get("growth_score") is not None and data.get("revenue_cagr") is not None:
            return text + f", 매출 연평균 성장률 {data['revenue_cagr']:.1%}(성장 {c5['growth_score']}점) → 상업화 70%·성장 30% 반영"
        return text + ", 3개년 매출 비교가 불가해 상업화 단계만 반영"
    if cid == "C6":
        areas = ((a.get("risk_analysis") or {}).get("areas")) or []
        seen = [f"{AREA_TEXT[x['category']]} 관찰 {len(x['observations'])}건·위험 신호 "
                f"{sum(o.get('kind') == 'risk_signal' for o in x['observations'])}건" for x in areas if x.get("observations")]
        if c.zero_filled or c.score is None or not seen:
            return "운영 관련 공개 관찰이 없어 판단하지 못함" + zero
        empty = [AREA_TEXT[x["category"]] for x in areas if not x.get("observations")]
        return ", ".join(seen) + (f" ({'·'.join(empty)}: 관찰 없음)" if empty else "") + " → 위험 신호 0건 5점·1건 3점·2건 이상 1점의 평균"
    return c.rationale


def score_reasons(review: InvestmentReview, analyses: dict) -> list[dict]:
    return [{"name": CRITERION_NAME[c.criterion_id], "weight": WEIGHTS[c.criterion_id], "score": c.score,
             "zero_filled": c.zero_filled, "reason": _reason(c.criterion_id, c, analyses)}
            for c in review.criterion_results]


def _criteria_rows(review: InvestmentReview) -> list[dict]:
    rows = []
    for c in review.criterion_results:
        rows.append({"id": c.criterion_id, "name": CRITERION_NAME[c.criterion_id], "weight": WEIGHTS[c.criterion_id],
                     "owner": CRITERION_OWNER[c.criterion_id], "score": c.score, "zero_filled": c.zero_filled,
                     "status": c.score_status.value, "rationale": c.rationale})
    return rows


GATE_TEXT = {"G01": "핵심 제품의 목표국 운영을 막는 공식 조치", "G02": "핵심 사업 운영의 현재 중단"}
GATE_SHORT = {"G01": "공식 규제 차단", "G02": "운영 중단"}
AGENT_KO = {"clinical": "임상·인허가", "market": "시장·사업성", "traction": "실적·성장성", "risk": "운영 리스크"}


EASY_SHORT = {"C1": "임상 연구", "C2": "시장 성장성", "C3": "병원·고객 도입 수요", "C4": "매출을 내는 구조",
              "C5": "매출·계약 실적", "C6": "운영 위험 대비"}


def _josa(word: str, pair: tuple[str, str] = ("을", "를")) -> str:
    """마지막 글자 받침 유무로 을/를 선택."""
    ch = word[-1] if word else ""
    return pair[0] if "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 else pair[1]


def explain(r: InvestmentReview, min_total: float = 60.0, min_criterion: float = 2.0) -> str:
    """부적격·판단불가 사유 (prompts/writer.md 4번 규칙을 코드로 구현). ① 결과 ② 0점 처리 이유."""
    codes = {c.value for c in r.reason_codes}
    first = []
    for g in r.gate_results:
        if g.code in codes:
            first.append(f"{GATE_TEXT[g.code]}가 확인되어 총점과 관계없이 부적격이다" + (f"({g.reason})" if g.reason else ""))
    if "SCORE_BELOW_60" in codes:
        first.append(f"총점 {r.total_score:g}점(100점 만점)으로 적격 기준인 {min_total:g}점에 못 미쳤다")
    if "CRITERION_BELOW_2" in codes:
        low = [EASY_SHORT[c.criterion_id] for c in r.criterion_results
               if c.score is not None and not c.zero_filled and c.score < min_criterion]
        first.append(f"{', '.join(low) or '일부 필수 항목'} 점수가 최소 기준({min_criterion:g}점)에 못 미쳤다")
    if r.final_status.value == "undetermined":
        causes = [g.reason for g in r.gate_results if g.status.value in ("not_checked", "unresolved") and g.reason]
        causes += [u.split(": ", 1)[-1] for u in r.remaining_unknowns
                   if re.match(r"^[^:]+:\w+:(as_of_mismatch|company_mismatch|unknown_evidence|no_evidence)", u)]
        first.append("판단에 필요한 공개자료가 부족해 판정하지 못했다" + (f"({'; '.join(dict.fromkeys(causes))})" if causes else ""))
    sentences = [". ".join(first) + "." if first else ""]
    zero = [c.criterion_id for c in r.criterion_results if c.zero_filled]
    if zero:
        if len(zero) >= 4:
            rest = [EASY_SHORT[c.criterion_id] for c in r.criterion_results if not c.zero_filled]
            head = f"{', '.join(rest)}{_josa(rest[-1], ('을', '를'))} 제외한 " if rest else ""
            sentences.append(f"{head}{len(zero)}개 항목의 공개자료를 찾지 못해 0점 처리됐다.")
        else:
            names = [EASY_SHORT[c] for c in zero]
            sentences.append(f"{', '.join(names)}{_josa(names[-1])} 보여주는 공개자료를 찾지 못해 0점 처리됐다.")
    return " ".join(x for x in sentences if x) or "-"


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
            "criteria": _criteria_rows(r), "score_reasons": score_reasons(r, a),
            "market": _market(a.get("market_analysis") or {}, ev, src, refs),
            "clinical": _clinical(a.get("clinical_analysis") or {}, ev, src, refs),
            "traction": _traction(a.get("traction_analysis") or {}, ev, src, refs),
            "risk": _risk(a.get("risk_analysis") or {}, src, refs),
            "concerns": _dedupe(r.report.concerns, 4),
            "questions": _dedupe(r.report.due_diligence_questions, MAX_QUESTIONS),
            "zero_filled": [CRITERION_NAME[c.criterion_id] for c in r.criterion_results if c.zero_filled],
            "zero_filled_ids": [c.criterion_id for c in r.criterion_results if c.zero_filled],
            "red_flag_codes": [f.get("code") for f in ((a.get("traction_analysis") or {}).get("data") or {}).get("red_flags") or []],
            "review_requests": r.review_request_ids, "review_responses": len(r.review_response_ids),
        })

    others = [{"company_id": r.company_id, "name": companies.get(r.company_id, {}).get("company_name", r.company_id),
               "status": r.final_status.value, "total": r.total_score, "rank": r.selection.rank,
               "reasons": [c.value for c in r.reason_codes], "explanation": explain(r),
               "zero_filled": [CRITERION_NAME[c.criterion_id] for c in r.criterion_results if c.zero_filled],
               "zero_filled_ids": [c.criterion_id for c in r.criterion_results if c.zero_filled],
               "low_criteria": [c.criterion_id for c in r.criterion_results
                                if c.score is not None and not c.zero_filled and c.score < 2]}
              for r in reviews_m if not r.selection.selected]

    status = Counter(r.final_status.value for r in reviews_m)
    zero = Counter(c.criterion_id for r in reviews_m for c in r.criterion_results if c.zero_filled)
    requests = Counter(i.split(":")[1] for r in reviews_m for i in r.review_request_ids)
    return {
        "meta": {**meta, "candidates": len(reviews_m), "selected": len(selected),
                 "status": {k: status.get(k, 0) for k in STATUS_KO}},
        "selected": selected,
        "others": others,
        "zero_filled_counts": {CRITERION_NAME[c]: zero.get(c, 0) for c in CRITERIA},
        "zero_filled_counts_by_id": {c: zero.get(c, 0) for c in CRITERIA},
        "reason_counts": dict(Counter(code.value for r in reviews_m for code in r.reason_codes)),
        "review": {"requests": {AGENT_KO.get(a, a): n for a, n in requests.items()}, "responses": sum(len(r.review_response_ids) for r in reviews_m)},
        "references": refs.items,
    }
