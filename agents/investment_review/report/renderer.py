"""보고서 Markdown 렌더링. 표·수치는 context에서 템플릿으로 직접 만들고, LLM 서술(narrative)은 선택이다.

목차 (설계서 27쪽): SUMMARY → 선정 기업 → 기술·임상·인허가 / 사업모델·실적 → 운영 리스크·평가표·선정 이유
                    → 보완 이력·한계·추가 실사 질문 → REFERENCE
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import unquote

from .context_builder import CRITERION_NAME, STATUS_KO

POLICY_KO = {"zero_fill": "미확인 항목 0점 처리", "partial": "부분 판정(채점 가중치 50% 이상)", "strict": "설계서 기준"}
STAGE_KO = {"A": "매출 확인", "B": "유료 계약", "C": "실증", "D": "MOU·수상", "none": "인정 근거 없음"}
AREA_KO = {"external_dependency": "외부 의존", "key_person_continuity": "핵심 인력", "operational_incidents": "운영 사건"}


def _refs(nums) -> str:
    return "".join(f"[{n}]" for n in nums or [])


def _num(v, digits=1) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _money(v, currency) -> str:
    if v is None:
        return "-"
    if currency == "KRW":
        return f"{v / 1e8:,.1f}억 원" if v >= 1e8 else f"{v / 1e6:,.0f}백만 원"
    return f"{v:,.0f} {currency or ''}".strip()


def _metric(m: dict) -> str:
    value = m["value"]
    if m["type"] == "cagr":
        rate = value / 100 if value > 1.5 else value  # 03이 %값(50.8)을 넣는 경우 대비
        text = f"CAGR {rate:.1%}"
    else:
        text = f"시장 규모 {value:,.2f} {m.get('unit') or ''}".strip()
    scope = " · ".join(x for x in (m.get("geography"), m.get("segment"), m.get("years")) if x)
    return f"{text} ({scope}){_refs(m['refs'])}"


def summary_text(ctx: dict) -> str:
    """LLM이 없거나 검증에 실패했을 때 쓰는 SUMMARY 본문."""
    m, s = ctx["meta"], ctx["meta"]["status"]
    names = ", ".join(f"{c['name']}({_num(c['total'])}점)" for c in ctx["selected"]) or "없음"
    zero = ", ".join(f"{c} {n}개" for c, n in ctx["zero_filled_counts"].items() if n)
    return (f"기준일 {m['as_of']} 기준 Healthcare AI 후보 {m['candidates']}개 기업을 임상·시장·실적·운영 4개 분야로 분석하고 "
            f"평가 기준 {m['criteria_version']}({POLICY_KO.get(m.get('policy'), m.get('policy'))})으로 판정했다. "
            f"적격 {s['eligible']}개, 부적격 {s['ineligible']}개, 판단불가 {s['undetermined']}개이며 "
            f"적격 기업 중 최대 {m['k']}개 선정 기준에 따라 {names}를 선정했다. "
            f"공개자료로 확인하지 못한 항목은 0점으로 처리했다({zero}). "
            f"따라서 점수는 기업의 실제 수준보다 공개 근거의 양을 크게 반영하며, 선정 기업도 추가 실사가 필요하다.")


def _company(c: dict, narrative: Optional[dict]) -> list[str]:
    mk, cl, tr, rk = c["market"], c["clinical"], c["traction"], c["risk"]
    out = [f"### {c['rank']}. {c['name']} — {_num(c['total'])}점",
           f"- 분야: {c.get('category') or '-'} · 대표 제품: {c.get('product') or '-'} · 핵심 기술: {c.get('technology') or '-'}"]
    point = (narrative or {}).get(c["company_id"])
    if point:
        out.append(f"- **핵심 검토 논점**: {point}")
    out += ["#### 고객 문제·시장",
            f"- 목표 고객 {mk.get('target_customer') or '-'} / 구매자 {mk.get('buyer') or '-'} / 사업모델 {mk.get('business_model') or '-'}",
            f"- 고객 문제: {mk.get('core_problem') or '-'}"]
    out += [f"- {_metric(m)}" for m in mk["metrics"]] or ["- 시장 규모·성장률 근거 없음"]
    out += ["#### 기술·임상·인허가"]
    if cl["products"]:
        out.append(f"- 제품: {' / '.join(cl['products'])}")
    out += [f"- 인허가: {r['country']} {r['authority']} {r['procedure']}·{r['status']} — {r.get('use') or ''}{_refs(r['refs'])}"
            for r in cl["regulatory"]] or ["- 공식 인허가 기록 없음 (미발견은 무허가 확정이 아님)"]
    out += [f"- 연구: {s.get('design') or '설계 미상'}{' · 전향적' if s.get('prospective') else ''}"
            f"{' · 다기관' if s.get('multicenter') else ''}{_refs(s['refs'])}" for s in cl["studies"]]
    out += [f"- 임상 경고: {f}" for f in cl["red_flags"]]
    out += ["#### 사업모델·실적·성장",
            f"- 상업화 단계: {STAGE_KO.get(tr.get('stage'), tr.get('stage') or '-')} · 투자 단계: {tr.get('invest_stage') or '-'}"
            f" · 고용 추이: {tr.get('headcount_trend') or '-'}"]
    if tr["revenue"]:
        cagr = f" (CAGR {tr['revenue_cagr']:.1%})" if tr.get("revenue_cagr") is not None else ""
        out.append("- 매출: " + ", ".join(f"{r['year']}년 {_money(r['value'], r['currency'])}{_refs(r['refs'])}"
                                          for r in tr["revenue"]) + cagr)
    out += [f"- 계약: {x['counterparty']}{' (유상)' if x.get('paid') else ''}{_refs(x['refs'])}" for x in tr["contracts"]]
    out += [f"- 실적 경고: {f}" for f in tr["red_flags"]]
    out += ["#### 운영 리스크 (공개자료 관찰, 사실 검증 전)"]
    for a in rk["areas"]:
        obs = "; ".join(f"{o['statement']}{' ⚠' if o['signal'] else ''}{_refs(o['refs'])}" for o in a["observations"])
        out.append(f"- {AREA_KO.get(a['category'], a['category'])}: {obs or '관찰 없음'}")
    out.append("")
    return out


def _score_table(selected: list[dict]) -> list[str]:
    """선정 기업 C1~C6 평가표 (항목 × 기업). 항목별 근거는 final_reviews.json의 rationale."""
    head = "| 항목 | 가중치 | " + " | ".join(c["name"] for c in selected) + " |"
    out = ["#### 선정 기업 C1~C6 평가표 (`0*` = 공개자료로 확인하지 못해 0점 처리)", "", head,
           "|---|---:|" + "---:|" * len(selected)]
    for i, r in enumerate(selected[0]["criteria"]):
        cells = ["0*" if c["criteria"][i]["zero_filled"] else _num(c["criteria"][i]["score"]) for c in selected]
        out.append(f"| {r['id']} {r['name']} | {r['weight']} | " + " | ".join(cells) + " |")
    out.append("| **총점** | 100 | " + " | ".join(f"**{_num(c['total'])}**" for c in selected) + " |")
    return out + [""]


def render(ctx: dict, narrative: Optional[dict] = None) -> str:
    m = ctx["meta"]
    narrative = narrative or {}
    lines = ["# Healthcare AI 스타트업 투자 검토 보고서", "",
             f"기준일 {m['as_of']} · 평가 기준 {m['criteria_version']} ({POLICY_KO.get(m.get('policy'), m.get('policy'))}) · "
             f"후보 {m['candidates']}개 · 선정 {m['selected']}개", "",
             "## 1. SUMMARY", "", narrative.get("summary") or summary_text(ctx), ""]

    lines += ["## 2. 선정 기업", ""]
    if not ctx["selected"]:
        lines += ["적격 기업이 없어 선정하지 않았다.", ""]
    for c in ctx["selected"]:
        lines += _company(c, narrative)
    if ctx["selected"]:
        lines += _score_table(ctx["selected"])

    lines += ["## 3. 전체 후보 판정", "", "`0*` 표시는 공개자료로 확인하지 못해 0점으로 처리한 항목이다.", "",
              "| 기업 | 판정 | 총점 | 사유 | 0점 처리 |", "|---|---|---:|---|---|"]
    for c in ctx["selected"]:
        lines.append(f"| {c['name']} | 적격·선정 {c['rank']}위 | {_num(c['total'])} | - | {', '.join(c['zero_filled']) or '-'} |")
    for o in ctx["others"]:
        state = STATUS_KO[o["status"]] + (f" {o['rank']}위(K 초과)" if o.get("rank") else "")
        lines.append(f"| {o['name']} | {state} | {_num(o['total'])} | {', '.join(o['reasons']) or '-'} | "
                     f"{', '.join(o['zero_filled']) or '-'} |")

    rv = ctx["review"]
    zero = ", ".join(f"{c} {n}" for c, n in ctx["zero_filled_counts"].items() if n)
    lines += ["", "## 4. 보완 이력·한계", "",
              f"- 보완 요청(기업당 1회): " + (", ".join(f"{a} {n}건" for a, n in sorted(rv["requests"].items())) or "없음")
              + f" · 응답 {rv['responses']}건",
              f"- 0점 처리 기업 수(항목별): {zero or '없음'}",
              "- C2는 세부시장 CAGR이 없으면 03이 찾은 상위 시장 CAGR 중앙값으로 채점했다(세부시장과 다를 수 있음).",
              "- C3·C4는 확인된 체크 수로 채점했고, 미확인 체크는 점수에 넣지 않았다.",
              "- C6는 운영 위험 신호 수로 채점했다(06 자체 규칙). 자료 없음은 위험 없음을 뜻하지 않는다.",
              "- 운영 리스크 관찰은 출처만 연결됐고 사실 검증 전이다. 회사 주장과 확인된 사실을 구분해 실사로 확인해야 한다.",
              "", "## 5. 추가 실사 질문", ""]
    for c in ctx["selected"]:
        lines.append(f"**{c['name']}**")
        lines += [f"- {q}" for q in c["questions"]] or ["- 없음"]
        lines.append("")

    lines += ["## REFERENCE", ""]
    for i, r in enumerate(ctx["references"], start=1):
        meta = ", ".join(x for x in (r.get("publisher"), r.get("published_at")) if x)
        title = (r.get("title") or "(제목 없음)").rstrip(".")[:80]
        lines.append(f"{i}. {title}" + (f". {meta}" if meta else "") + f". {unquote(r.get('url') or '')}")
    if not ctx["references"]:
        lines.append("선정 기업 서술에 인용한 출처 없음.")
    return "\n".join(lines) + "\n"
