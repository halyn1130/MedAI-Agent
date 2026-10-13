"""실적·성장성 — 판정 규칙 (criteria.md 1장). 순수 함수: 같은 관측값이면 같은 결과, 외부 호출·LLM 없음."""
from __future__ import annotations

from datetime import date
import re
from typing import Iterable, Optional

from . import schema as S
from .common import (
    add_months,
    in_window,
    parse_date,
)

# ════════════════════════════════════════════════════════════
# 4. 판정 규칙 (criteria.md 1장) — 순수 함수, 같은 관측값이면 같은 결과
# ════════════════════════════════════════════════════════════
STAGE_RANK = {"none": 0, "D": 1, "C": 2, "B": 3, "A": 4}
EXCLUDED_CONTRACT = {"cancelled", "terminated"}


def _ok(ids: Iterable[str], confirmed: set[str]) -> bool:
    return any(i in confirmed for i in ids or [])


def _paid_recognized(c: dict, confirmed: set[str]) -> bool:
    return (c.get("is_paid") is True and c.get("independent_confirmation") is True
            and c.get("contract_status") not in EXCLUDED_CONTRACT and _ok(c.get("evidence_ids"), confirmed))


# ─────────────────────────────────────────────
# 1-3. 상업화 단계
# ─────────────────────────────────────────────
def commercial_stage(as_of: date, revenue: list, contracts: list, activities: list,
                     confirmed: set[str]) -> tuple[str, list[str], list[str]]:
    """(단계, 근거 evidence_ids, 날짜 경계 메모)."""
    start = add_months(as_of, -S.STAGE_WINDOW_MONTHS)
    cands: dict[str, list[str]] = {"A": [], "B": [], "C": [], "D": []}
    notes: list[str] = []

    def put(level, date_str, ids, label):
        w = in_window(date_str, start, as_of)
        if w == "in":
            cands[level].extend(ids)
        elif w == "uncertain":
            notes.append(f"{label}({date_str})가 {S.STAGE_WINDOW_MONTHS}개월 경계에 걸려 포함 여부 미확정")

    for r in revenue:
        if (r.get("value") or 0) > 0 and _ok(r.get("evidence_ids"), confirmed):
            put("A", r.get("period_end"), r["evidence_ids"], f"{r.get('fiscal_year')}년 매출")
    for c in contracts:
        if _paid_recognized(c, confirmed):
            put("B", c.get("event_date"), c["evidence_ids"], f"{c.get('counterparty_name')} 유료 계약")
    for a in activities:
        if not _ok(a.get("evidence_ids"), confirmed):
            continue
        level = {"poc": "C", "mou": "D", "award": "D"}.get(a.get("type"))
        if level:
            put(level, a.get("date"), a["evidence_ids"], f"{a.get('type')}")

    for level in ("A", "B", "C", "D"):
        if cands[level]:
            return level, sorted(set(cands[level])), notes
    return "none", [], notes


# ─────────────────────────────────────────────
# 1-4. 매출 시계열 · CAGR · 성장 구간
# ─────────────────────────────────────────────
def revenue_by_year(revenue: list, confirmed: set[str], as_of: date) -> tuple[dict, Optional[str]]:
    """회사 전체·인정 근거·같은 회계 범위만 모은 연도별 매출.
    범위는 연도가 많은 쪽, 동률이면 연결(자회사로 사업을 옮긴 경우 별도 매출만 보면 역성장으로 오판)."""
    groups: dict[str, dict[int, dict]] = {}
    for r in revenue:
        if r.get("product_id") or r.get("value") is None or not _ok(r.get("evidence_ids"), confirmed):
            continue
        scope = r.get("accounting_scope") or "unknown"
        if scope == "unknown":
            continue                                   # 범위 불명 값은 같은 시계열에 넣지 않음
        end, _ = parse_date(r.get("period_end"))
        if end is None or end > as_of:
            continue
        g = groups.setdefault(scope, {})
        g.setdefault(int(r["fiscal_year"]), {
            "value": r["value"], "currency": r.get("currency", "KRW"),
            "accounting_scope": scope, "evidence_ids": list(r.get("evidence_ids", [])),
        })
    if not groups:
        return {}, None
    scope = max(groups, key=lambda k: (len(groups[k]), k == "consolidated"))
    return dict(sorted(groups[scope].items())), scope


def growth_tier(r: Optional[float]) -> str:
    if r is None:
        return "G0"
    if r >= S.GROWTH_G1_MIN:
        return "G1"
    if r >= S.GROWTH_G2_MIN:
        return "G2"
    if r >= 0:
        return "G3"
    return "G4"


RESTATE_TOL = 0.005   # 같은 연도 값이 보고서 간 0.5% 넘게 다르면 기준이 다른 값(정정·연결범위 변경)으로 본다


def cagr(by_year: dict, report_values: Optional[dict] = None) -> tuple[Optional[float], str, dict]:
    """(CAGR, 성장 구간, growth_detail). PDF 05: 동일 범위의 연속 3개 결산연도, 시작 > 0, 마지막 ≥ 0일 때
    (last/first)^(1/2) - 1. 누락·음수·범위 차이는 null/G0.

    report_values: {보고서 키: {연도: 값}} — 같은 연도 값이 보고서마다 다르면(정정·연결범위 변경)
    같은 기준의 시계열이 아니므로 '범위 차이'로 보고 G0 처리한다."""
    years = sorted(y for y, v in by_year.items() if v.get("value") is not None)
    detail = {"observed_years": years, "periods": 0, "unavailable_reason": None}
    if not years:
        detail["unavailable_reason"] = "연도별 매출 관측 없음"
        return None, "G0", detail
    last_y = years[-1]
    need = [last_y - 2, last_y - 1, last_y]
    missing = [y for y in need if y not in years]
    if missing:
        detail["unavailable_reason"] = f"연속 3개 결산연도 부족 (누락: {', '.join(map(str, missing))})"
        return None, "G0", detail
    first, last = by_year[need[0]]["value"], by_year[last_y]["value"]
    if len({by_year[y].get("currency") for y in need}) > 1:
        detail["unavailable_reason"] = "통화 불일치"
        return None, "G0", detail
    restated = restated_years(need, report_values)
    if restated:
        detail["unavailable_reason"] = (f"범위 차이: {', '.join(map(str, restated))}년 매출이 감사보고서마다 다름"
                                        "(정정·연결범위 변경) — 같은 기준의 3개년 시계열 아님")
        return None, "G0", detail
    if first <= 0:
        detail["unavailable_reason"] = f"시작 연도({need[0]}) 매출 ≤ 0"
        return None, "G0", detail
    if last < 0:
        detail["unavailable_reason"] = f"마지막 연도({last_y}) 매출 음수"
        return None, "G0", detail
    r = round((last / first) ** 0.5 - 1, 4)
    detail["periods"] = 2
    detail["observed_years"] = need
    return r, growth_tier(r), detail


def restated_years(years: list[int], report_values: Optional[dict]) -> list[int]:
    out = []
    for y in years:
        vals = [rv[y] for rv in (report_values or {}).values() if y in rv]
        if len(vals) > 1 and max(vals) - min(vals) > RESTATE_TOL * max(abs(v) for v in vals):
            out.append(y)
    return out


# ─────────────────────────────────────────────
# 1-4. 고용·고객 추이
# ─────────────────────────────────────────────
def trend(observations: list[tuple], as_of: date) -> tuple[str, dict]:
    """observations: (date_str, value, group_key, evidence_ids) 목록.
    최신 관측 as_of 이전 3개월 이내, 비교값 약 12개월 전(±1개월), 같은 group, 기준값 > 0."""
    detail = {"change_rate": None, "period_start": None, "period_end": None,
              "definition": None, "evidence_ids": [], "unavailable_reason": None}
    obs = []
    for ds, v, g, ids in observations:
        d, _ = parse_date(ds)
        if d is not None and v is not None:
            obs.append((d, v, g, ids, ds))
    lo = add_months(as_of, -S.TREND_LATEST_MAX_AGE_MONTHS).replace(day=1)   # 관측이 월초 날짜라 월 단위로 비교
    latest = [o for o in obs if lo <= o[0] <= as_of]
    if not latest:
        detail["unavailable_reason"] = f"최신 관측이 기준일 {S.TREND_LATEST_MAX_AGE_MONTHS}개월 이내에 없음"
        return "unknown", detail
    L = max(latest, key=lambda o: o[0])
    gmin, gmax = S.TREND_BASE_GAP_MONTHS
    b_lo, b_hi, target = add_months(L[0], -gmax), add_months(L[0], -gmin), add_months(L[0], -12)
    bases = [o for o in obs if o[2] == L[2] and b_lo <= o[0] <= b_hi]
    if not bases:
        detail["unavailable_reason"] = "같은 정의의 약 12개월 전(±1개월) 비교값 없음"
        return "unknown", detail
    B = min(bases, key=lambda o: abs((o[0] - target).days))
    detail.update(period_start=B[4], period_end=L[4], definition=L[2],
                  evidence_ids=sorted(set(B[3]) | set(L[3])))
    if B[1] <= 0:
        detail["unavailable_reason"] = "기준값 ≤ 0"
        return "unknown", detail
    rate = round((L[1] - B[1]) / B[1], 4)
    detail["change_rate"] = rate
    if rate >= S.TREND_UP_MIN:
        return "up", detail
    if rate <= -S.TREND_UP_MIN:
        return "down", detail
    return "flat", detail


# ─────────────────────────────────────────────
# 1-5. 투자 단계 기대치
# ─────────────────────────────────────────────
def normalize_invest_stage(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = re.sub(r"[\s_]", "", str(raw)).lower().replace("시리즈", "series").replace("프리", "pre-")
    t = re.sub(r"pre-?series-?", "pre-", t)          # 'Pre-Series A'·'프리시리즈A' → 'pre-a'
    if "ipo" in t:
        return "Pre-IPO"
    if re.search(r"pre-?seed", t):
        return "Pre-seed"
    if "seed" in t or "시드" in t or "엔젤" in t:
        return "Seed"
    m = re.search(r"pre-?([a-e])\b|pre-?([a-e])$", t)
    if m:
        letter = (m.group(1) or m.group(2)).upper()
        key = f"Pre-{letter}"
        return key if key in S.STAGE_EXPECTATION else None
    m = re.search(r"series-?([a-e])", t) or re.fullmatch(r"([a-e])\+?", t)
    if m:
        key = f"Series {m.group(1).upper()}"
        return key if key in S.STAGE_EXPECTATION else None
    return None


def expectation(invest_stage: Optional[str], stage: str, none_reason: Optional[str]) -> tuple[str, Optional[str]]:
    key = normalize_invest_stage(invest_stage)
    if key is None:
        return "unknown", None
    req = S.STAGE_EXPECTATION[key]
    if stage == "none":
        # 4번 정의: 조회를 끝냈는데 인정 근거가 없으면 미달, 조사 미완료면 판단 불가
        return ("not_met" if none_reason == "no_recognized_evidence" else "unknown"), req
    return ("met" if STAGE_RANK[stage] >= STAGE_RANK[req] else "not_met"), req


# ─────────────────────────────────────────────
# 1-6. 레드플래그
# ─────────────────────────────────────────────
def red_flags(as_of: date, contracts: list, activities: list, confirmed: set[str],
              tier: str, revenue_cagr: Optional[float], growth_ids: list[str],
              headcount_detail: dict, expectation_status: str, invest_stage: Optional[str],
              stage: str, required: Optional[str], contract_search_ok: bool) -> tuple[list[dict], list[str]]:
    """(flags[{code, reason, evidence_ids, status}], 날짜 경계 메모). 조건은 PDF 05 레드플래그 표 그대로."""
    flags, notes = [], []
    start24 = add_months(as_of, -S.RF_WINDOW_MONTHS)
    period = f"{start24.isoformat()}~{as_of.isoformat()}"

    paid = [c for c in contracts if _paid_recognized(c, confirmed)]
    paid_in = [c for c in paid if in_window(c.get("event_date"), start24, as_of) == "in"]
    paid_unc = [c for c in paid if in_window(c.get("event_date"), start24, as_of) == "uncertain"]
    for c in paid_unc:
        notes.append(f"{c.get('counterparty_name')} 계약({c.get('event_date')})이 24개월 경계에 걸림 → RF1 확정 보류")

    # RF1
    if contract_search_ok and not paid_in and not paid_unc:
        flags.append({"code": "RF1", "status": "needs_review", "evidence_ids": [],
                      "reason": f"기준일 직전 24개월({period})에 인정 신규 유료 계약을 찾지 못함 (계약 없음 확정 아님)"})

    # RF2
    rf2_reasons, rf2_ids = [], []
    if tier == "G4":
        rf2_reasons.append(f"비교 가능한 매출 감소 (CAGR {revenue_cagr:.1%}, G4)")
        rf2_ids += growth_ids
    hc = headcount_detail.get("change_rate")
    if hc is not None and hc <= S.RF2_HEADCOUNT_DROP:
        rf2_reasons.append(f"12개월 고용 감소율 {hc:.1%} ({headcount_detail.get('period_start')}→{headcount_detail.get('period_end')})")
        rf2_ids += headcount_detail.get("evidence_ids", [])
    if rf2_reasons:
        flags.append({"code": "RF2", "status": "confirmed", "evidence_ids": sorted(set(rf2_ids)),
                      "reason": " / ".join(rf2_reasons) + " — 수치 사실, 원인은 실사 대상"})

    # RF3
    def in24(x, key):
        return in_window(x.get(key), start24, as_of) == "in"
    mous = [a for a in activities if a.get("type") == "mou" and _ok(a.get("evidence_ids"), confirmed) and in24(a, "date")]
    # 상대 기관이 없는 MOU 기사는 같은 달이면 같은 MOU로 본다 (기사 여러 건 = 협약 여러 건 아님)
    distinct = {a.get("counterparty_id") or ("month", (a.get("date") or "")[:7]) for a in mous}
    pocs = [a for a in activities if a.get("type") == "poc" and _ok(a.get("evidence_ids"), confirmed) and in24(a, "date")]
    if len(distinct) >= S.RF3_MIN_MOU and not paid_in and not pocs:
        flags.append({"code": "RF3", "status": "needs_review",
                      "evidence_ids": sorted({i for a in mous for i in a.get("evidence_ids", [])}),
                      "reason": f"24개월 내 서로 다른 MOU {len(distinct)}건, 인정 유료 계약·실증 미발견"})

    # RF4
    if expectation_status == "not_met":
        flags.append({"code": "RF4", "status": "needs_review", "evidence_ids": [],
                      "reason": f"투자 단계 {invest_stage}의 최소 기대 {required} 대비 상업화 단계 {stage}"})
    return flags, notes


# ─────────────────────────────────────────────
# 1-7. C5 점수 입력
# ─────────────────────────────────────────────
def c5(stage: str, tier: str, evidence_ids: list[str]) -> dict:
    s = S.C5_COMMERCIAL_SCORE.get(stage)
    t = S.C5_GROWTH_SCORE.get(tier)
    w_s, w_t = S.C5_WEIGHTS
    out = {"score": None, "score_status": "unknown", "commercial_score": s, "growth_score": t,
           "growth_metric": "revenue_cagr" if t is not None else None,
           "growth_status": "scored" if t is not None else "unknown",
           "evidence_ids": sorted(set(evidence_ids)), "rationale": ""}
    if s is None:
        out["rationale"] = "상업화 인정 근거 없음(none) → 점수 판단불가, 6번이 처리"
        return out
    if t is None:
        out.update(score=float(s), score_status="scored",
                   rationale=f"상업화 {stage}(S={s}). 성장 자료 미확인(G0)으로 S만 사용")
        return out
    out.update(score=round(w_s * s + w_t * t, 2), score_status="scored",
               rationale=f"상업화 {stage}(S={s}) · 성장 {tier}(T={t}) → {w_s}·S + {w_t}·T")
    return out


