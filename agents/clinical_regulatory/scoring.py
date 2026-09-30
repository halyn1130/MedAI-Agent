import re

try:
    from . import schema as S
    from .config import C1_LEVEL_SCORE, C1_NOT_APPLICABLE_TYPES, LEVEL_RANK
    from .context import ClinicalRun
    from .utils import _company_core, _dedupe, _norm_txt, _pkey, compare_to_as_of
except ImportError:
    import schema as S
    from config import C1_LEVEL_SCORE, C1_NOT_APPLICABLE_TYPES, LEVEL_RANK
    from context import ClinicalRun
    from utils import _company_core, _dedupe, _norm_txt, _pkey, compare_to_as_of


# ══════════════════════════════════════════════
# 점수·레드플래그·Finding·결측 (코드 규칙)
# ══════════════════════════════════════════════
def _scoreable(ctx: ClinicalRun, study: dict) -> bool:
    """발행일이 기준일 이전으로 확인된 출처가 있어야 점수 근거로 사용"""
    for eid in study["evidence_ids"]:
        for sid in ctx.evidence.get(eid, {}).get("source_ids", []):
            if compare_to_as_of(ctx.sources.get(sid, {}).get("published_at"), ctx.as_of) == "before":
                return True
    return False


def _is_company_affiliated_study(ctx: ClinicalRun, study: dict) -> bool:
    """논문 출처(제목, 저자, 초록 등)에 회사명이 명시되어 있는지 확인"""
    texts = []
    for eid in study.get("evidence_ids", []):
        ev = ctx.evidence.get(eid, {})
        texts += [ev.get("statement", ""), ev.get("excerpt", "")]
        for sid in ev.get("source_ids", []):
            texts += [ctx.sources.get(sid, {}).get("title", ""), ctx.source_text.get(sid, "")]
    blob = _norm_txt(" ".join(t for t in texts if t)).lower()
    c_names = ctx.company_names()
    return any(
        (_company_core(cn) and len(_company_core(cn)) >= 3 and _company_core(cn) in blob)
        or (_norm_txt(cn).lower() and len(_norm_txt(cn).lower()) >= 3 and _norm_txt(cn).lower() in blob)
        for cn in c_names
    )


def _study_names_product(ctx: ClinicalRun, study: dict) -> bool:
    """논문 제목·요약 근거에 제품명(브랜드·모델)이 실제로 나오는지 (동등성 근거 없는 회사 연구는 합치지 않음)"""
    pnames = ctx.product_names(study["product_id"])
    texts = []  # LLM이 쓴 endpoint·result·statement는 제외 (제품명을 끼워 넣을 수 있음)
    for eid in study.get("evidence_ids", []):
        for sid in ctx.evidence.get(eid, {}).get("source_ids", []):
            texts += [ctx.sources.get(sid, {}).get("title", ""), ctx.source_text.get(sid, "")]
    combined_raw = " ".join(t for t in texts if t)
    blob = _pkey(combined_raw)

    is_affiliated = _is_company_affiliated_study(ctx, study)
    for n in pnames:
        if not n or len(n.strip()) < 2:
            continue
        clean_n = n.strip()
        # 4자 이하 짧은 명칭의 오탐(예: MARS 척도) 방지: 단어 경계 매칭 + 회사명/소속 동시 확인
        if len(clean_n) <= 4:
            pattern = rf"\b{re.escape(clean_n)}\b"
            if re.search(pattern, combined_raw, re.IGNORECASE) and is_affiliated:
                return True
        else:
            if re.search(rf"\b{re.escape(clean_n)}\b", combined_raw, re.IGNORECASE) or _pkey(clean_n) in blob:
                return True
    return False


def c1_for_product(ctx: ClinicalRun, pid: str) -> dict:
    info = ctx.classify[pid]
    has_claim = info.get("has_efficacy_claim")
    has_claim = bool(ctx.product(pid).claims) if has_claim is None else has_claim
    checks = [c for c in ctx.claim_checks if c["product_id"] == pid]
    if checks and info["regulatory_applicability"] == "out_of_scope":
        has_claim = any("효능 주장 아님" not in c["comparison_reason"] for c in checks)
        # 분류 상태와 C1 사유의 일관성 동기화
        info["has_efficacy_claim"] = has_claim

    if not has_claim and (info["regulatory_applicability"] == "out_of_scope" or
                          (info["product_type"] in C1_NOT_APPLICABLE_TYPES and info["regulatory_applicability"] != "in_scope")):
        return {"score": None, "status": "not_applicable", "level": "L0", "evidence_ids": [], "conflict": False,
                "rationale": "규제 비대상이며 효능 주장이 없어 임상 효능 평가 비적용 (전체 후보군 공통 규칙)"}

    rel = [s for s in ctx.studies if s["product_id"] == pid and s["product_match"]]

    # L3 검증: 전향적 다기관(prospective and multicenter) 또는 독립 외부 검증(external_validation)이 아니면 L2 상한
    for s in rel:
        if s.get("evidence_level") == "L3":
            is_prospective_multi = (s.get("prospective") is True and s.get("multicenter") is True)
            is_ext_val = (s.get("external_validation") is True)
            if not (is_prospective_multi or is_ext_val):
                s["evidence_level"] = "L2"

    # 결과 미발표/진행 중인 연구(unclear, mixed 등)는 점수 산정에서 제외
    def _is_reported(s: dict) -> bool:
        if s.get("result_direction") not in ("supports", "contradicts"):
            return False
        unreported_kws = ("진행 중", "예정", "모집 중", "모집 완료", "미발표", "발표되지 않", "결과 없음",
                          "ongoing", "recruiting", "not yet published", "planned", "protocol")
        text = ((s.get("result") or "") + " " + (s.get("endpoint") or "") + " " + (s.get("limitations") or "")).lower()
        return not any(kw in text for kw in unreported_kws)

    reported = [s for s in rel if _is_reported(s)]

    named = []
    for s in reported:
        if _study_names_product(ctx, s):
            named.append(s)
        elif _is_company_affiliated_study(ctx, s):
            # 회사 소속 논문은 제품명이 명시되지 않아도 L2 상한으로 점수 인정
            capped_level = "L2" if LEVEL_RANK.get(s["evidence_level"], 0) > LEVEL_RANK["L2"] else s["evidence_level"]
            named.append({**s, "evidence_level": capped_level})

    usable = [s for s in named if _scoreable(ctx, s)]
    notes = []
    if len(rel) - len(reported):
        notes.append(f"결과 미발표/진행 중으로 {len(rel) - len(reported)}건 점수 제외")
    if len(reported) - len(named):
        notes.append(f"제품명 미기재 및 비소속 연구로 {len(reported) - len(named)}건 점수 제외")
    if len(named) - len(usable):
        notes.append(f"발행일 미상·기준일 판단불가로 {len(named) - len(usable)}건 점수 제외")
    note = f" ({'; '.join(notes)})" if notes else ""
    if not usable:
        return {"score": None, "status": "unknown", "level": "L0", "evidence_ids": [], "conflict": False,
                "rationale": "평가 가능한 제품 관련 임상 근거를 찾지 못함" + note}

    major = [s for s in usable if LEVEL_RANK[s["evidence_level"]] >= 2]
    sup = [s for s in major if s["result_direction"] == "supports"]
    con = [s for s in major if s["result_direction"] == "contradicts"]
    if sup and con:
        return {"score": None, "status": "unknown", "level": max((s["evidence_level"] for s in major), key=LEVEL_RANK.get),
                "evidence_ids": [e for s in sup + con for e in s["evidence_ids"]], "conflict": True,
                "rationale": "주요 근거 간 결과 충돌 (CL03) — 해결 전 판단불가" + note}
    if con and not sup:
        return {"score": 0, "status": "scored", "level": max((s["evidence_level"] for s in con), key=LEVEL_RANK.get),
                "evidence_ids": [e for s in con for e in s["evidence_ids"]], "conflict": False,
                "rationale": "관련 주요 근거에서 핵심 효능에 반하는 결론이 일관되게 확인됨" + note}

    candidates = [s for s in usable if s["result_direction"] == "supports"
                  and (s["evidence_level"] == "L1" or s["methods_verifiable"])]
    if not candidates:
        return {"score": None, "status": "unknown", "level": max((s["evidence_level"] for s in usable), key=LEVEL_RANK.get),
                "evidence_ids": [e for s in usable for e in s["evidence_ids"]], "conflict": False,
                "rationale": "방법·결과 해석이 불충분하여 판단불가" + note}
    best = max(candidates, key=lambda s: LEVEL_RANK[s["evidence_level"]])
    lvl = best["evidence_level"]
    return {"score": C1_LEVEL_SCORE[lvl], "status": "scored", "level": lvl,
            "evidence_ids": [e for s in candidates if s["evidence_level"] == lvl for e in s["evidence_ids"]],
            "conflict": False, "rationale": f"가장 높은 유효 근거 수준 {lvl}" + note}


def aggregate_c1(ctx: ClinicalRun, per: dict[str, dict]) -> dict:
    """제품별 점수를 scope_weight 가중평균으로 잠정 집계 (최종 집계는 심사 p.4 규칙)"""
    ps = [S.ProductScore(product_id=pid, score=r["score"], score_status=r["status"],
                         scope_weight=ctx.product(pid).scope_weight) for pid, r in per.items()]
    scored = [p for p in ps if p.score_status == "scored"]
    ev = _dedupe([e for r in per.values() for e in r["evidence_ids"]])
    if not scored:
        status = "not_applicable" if ps and all(p.score_status == "not_applicable" for p in ps) else "unknown"
        return S.CriterionInput(score=None, score_status=status, product_scores=ps, evidence_ids=ev,
                                rationale="점수화된 제품 없음").model_dump()
    w = sum(p.scope_weight for p in scored) or 1.0
    score = round(sum(p.score * p.scope_weight for p in scored) / w, 2)
    unknown = [p.product_id for p in ps if p.score_status == "unknown"]
    rationale = "제품별 점수의 scope_weight 가중평균 (잠정, 심사 p.4 규칙으로 재집계)"
    if unknown:
        rationale += f" / 판단불가 제품 제외: {', '.join(unknown)}"
    return S.CriterionInput(score=score, score_status="scored", product_scores=ps, evidence_ids=ev,
                            rationale=rationale).model_dump()


def _build_missing(ctx: ClinicalRun, per: dict[str, dict]) -> list[dict]:
    items: list[tuple] = []  # (field, cause, route, impact, detail)
    c = ctx.company
    # 입력(CSV)이 고정이고 이 담당이 직접 수집했으므로, 수집 실패는 공개자료 미발견 → 실사 경로
    in_cause, in_route = ("not_found", "due_diligence") if ctx.self_collected else ("input_missing", "profile")
    if not c.legal_name:
        items.append(("company_profile.legal_name", in_cause, in_route, "high",
                      "법인 정식명을 확인하지 못해 식약처 DB를 표시명으로만 조회함"))
    for p in c.products:
        pid, appl = p.product_id, ctx.classify[p.product_id]["regulatory_applicability"]
        has_record = any(r["product_id"] == pid and r["status"] not in ("not_found", "unknown", "not_applicable")
                         for r in ctx.reg_records)
        if appl != "out_of_scope" and not p.model and not has_record:  # 허가 기록을 찾았으면 모델명 없어도 문제 없음
            items.append((f"products[{pid}].model", in_cause, in_route, "medium", "모델명이 없어 품목 매칭 정확도가 낮음"))
        if not p.claims and (appl != "out_of_scope" or ctx.classify[pid].get("has_efficacy_claim")):
            others = any(q.claims for q in c.products if q.product_id != pid)
            items.append((f"products[{pid}].claims", in_cause, in_route, "low" if others else "medium",
                          "이 제품에 대한 회사 주장 원문을 찾지 못해 주장 대조 불가"))
        primary = p.target_countries[0] if p.target_countries else "KR"
        for r in ctx.reg_records:
            if r["product_id"] == pid and r["status"] == "not_found" and appl != "out_of_scope":
                cov = ctx.coverage.get(("regulatory", pid, r["country"]), {})
                names = ", ".join(cov.get("search_scope", {}).get("names", [])) or "-"
                items.append((f"regulatory_records[{pid}][{r['country']}]", "not_found", "due_diligence",
                              "high" if r["country"] == primary else "medium",
                              f"조회 범위 내 미발견 (조회명: {names}, 확인일 {ctx.checked_at}). 공개자료로 해결되지 않으면 실사 필요"))
        if per[pid]["status"] == "unknown" and appl != "out_of_scope":
            items.append((f"criteria_inputs.C1[{pid}]", "not_found", "due_diligence", "high", per[pid]["rationale"]))
    impact_by_area = {"classification": "high", "regulatory": "high", "clinical_studies": "high",
                      "claim_check": "medium", "designation": "medium", "reimbursement": "medium"}
    for cov in ctx.coverage.values():
        if cov["status"] in ("search_failed", "not_reviewed"):
            field = f"{cov['area']}[{cov.get('product_id')}]" + (f"[{cov['country']}]" if cov.get("country") else "")
            impact = impact_by_area[cov["area"]]
            if cov["area"] == "regulatory" and cov.get("country") not in (None, "KR"):
                impact = "medium"
            items.append((field, cov["status"], "clinical", impact,
                          "외부 조회 실패" if cov["status"] == "search_failed" else "호출 예산·단계 제외로 검토하지 않음"))
    out, seen = [], set()
    for field, cause, route, impact, detail in items:
        if (field, cause) in seen:
            continue
        seen.add((field, cause))
        iid = next((m["item_id"] for m in ctx.prev_missing if m["field"] == field), None) or ctx.new_id("mi")
        out.append(S.MissingItem(item_id=iid, field=field, cause=cause, route=route, impact=impact,
                                 detail=detail).model_dump())
    return out


def _build_summary(ctx: ClinicalRun, per: dict[str, dict], names: dict[str, str]) -> str:
    lines = [f"[임상·인허가] {ctx.company.display_name} ({ctx.cid}) | 기준일 {ctx.as_of}"]
    for pa in ctx.product_assessments:
        pid = pa["product_id"]
        regs = [f"{r['country']} {r['procedure_type']}·{r['status']}" + (f"(\"{r['permitted_use']}\")" if r.get("permitted_use") else "")
                for r in ctx.reg_records if r["product_id"] == pid]
        progs = [f"{r['program']}·{r['status']}" for r in ctx.designations + ctx.reimbursements if r["product_id"] == pid]
        claims = [c["status"] for c in ctx.claim_checks if c["product_id"] == pid]
        claim_txt = ", ".join(f"{s} {claims.count(s)}" for s in _dedupe(claims)) or "주장 없음"
        score = pa["clinical_score"] if pa["clinical_score"] is not None else "없음"
        lines.append(
            f"- {pid} {names[pid]}: {pa['product_type']}/{pa['regulatory_applicability']} | "
            f"공식 상태: {'; '.join(regs) or '기록 없음'} | 제도: {'; '.join(progs) or '확인된 기록 없음'} | "
            f"임상 근거 {pa['evidence_level']}, C1 {score} ({pa['score_status']}) | 주장 대조: {claim_txt} | "
            f"평가 상태 {pa['assessment_status']}")
    c1 = ctx.c1
    lines.append(f"- C1 잠정 집계: {c1['score'] if c1['score'] is not None else '없음'} ({c1['score_status']})")
    if ctx.red_flags:
        lines.append("- 레드플래그 후보: " + "; ".join(f"{f['code']}({f['materiality']})" for f in ctx.red_flags))
    high = [m["field"] for m in ctx.missing_items if m["impact"] == "high"]
    if high:
        lines.append("- 주요 한계: " + ", ".join(high))
    return "\n".join(lines)
