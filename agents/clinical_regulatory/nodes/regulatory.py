from __future__ import annotations

import re
from typing import Optional

try:
    from .. import schema as S
    from ..config import MFDS_ITEM, MFDS_ITEM_MAX_PER_COMPANY, MFDS_PERMIT
    from ..context import ClinicalAgentState, ClinicalRun
    from ..llm import _company_brief, _product_brief, ask
    from ..tools import _coverage_status, _item_values, _scope_of, mfds_query, openfda_510k, web_search
    from ..utils import (
        _clean,
        _company_core,
        _dedupe,
        _mentioned_products,
        _norm_txt,
        _pkey,
        compare_to_as_of,
        norm_date,
        strip_fake_jan1,
    )
except ImportError:
    import schema as S
    from config import MFDS_ITEM, MFDS_ITEM_MAX_PER_COMPANY, MFDS_PERMIT
    from context import ClinicalAgentState, ClinicalRun
    from llm import _company_brief, _product_brief, ask
    from tools import _coverage_status, _item_values, _scope_of, mfds_query, openfda_510k, web_search
    from utils import (
        _clean,
        _company_core,
        _dedupe,
        _mentioned_products,
        _norm_txt,
        _pkey,
        compare_to_as_of,
        norm_date,
        strip_fake_jan1,
    )


def _is_mismatched_item(prod: S.ProductInput, permit_item_or_use: str) -> bool:
    """제품 특성과 식약처 품목명/사용목적이 완전히 상충하는지 확인"""
    if not permit_item_or_use:
        return False
    txt = permit_item_or_use.lower()
    p_name = (prod.name or "").lower()
    p_use = (prod.intended_use or "").lower()
    p_mod = (prod.modality or "").lower()

    # 1. 시약 vs 장치/기구 (예: 일반면역검사시약 vs 의료용분광광도장치)
    is_reagent_prod = any(k in p_name or k in p_use or k in p_mod for k in ("시약", "reagent", "키트", "진단키트"))
    is_device_permit = any(k in txt for k in ("장치", "기구", "장비", "기기", "분광광도", "가이드"))
    is_reagent_permit = any(k in txt for k in ("시약", "reagent", "체외진단용"))
    if is_reagent_prod and is_device_permit and not is_reagent_permit:
        return True

    # 2. 순수 소프트웨어/AI vs 물리 기구/소모품 (예: AI 알고리즘 vs 수술 가이드/주사기)
    is_sw_prod = any(k in p_name or k in p_use or k in p_mod for k in ("소프트웨어", "ai", "알고리즘", "솔루션", "플랫폼"))
    is_hw_only_permit = any(k in txt for k in ("주사기", "장갑", "침", "가이드", "카테터", "밴드", "소모품"))
    if is_sw_prod and is_hw_only_permit and "소프트웨어" not in txt:
        return True

    return False


def _reassign_product(ctx: ClinicalRun, pid: str, *texts: Optional[str]) -> str:
    """기록의 범위·근거 문장에 다른 제품명이 명시돼 있으면 그 제품으로 연결 (예: 'LuCAS Plus' 지정 → LuCAS Plus)"""
    prods = ctx.company.products
    full_text = " ".join(t for t in texts if t)
    hit = _mentioned_products(full_text, prods)
    if len(hit) == 1 and prods[hit[0]].product_id != pid:
        return prods[hit[0]].product_id

    # 모달리티/용도 키워드 기반 불일치 및 재매핑 (예: 초음파 ↔ CT, 인공와우 ↔ 가이드)
    norm_txt = _norm_txt(full_text).lower()
    for p in prods:
        if p.product_id == pid:
            continue
        # 타 제품의 주요 키워드가 원문에 있고 현재 제품과 상충하는 경우
        p_keys = [_pkey(p.name), _pkey(p.model), _pkey(p.modality)]
        if any(k and len(k) >= 2 and k in norm_txt for k in p_keys):
            curr_p = ctx.product(pid)
            # 현재 제품의 모달리티/이름과 명백히 다르고 타 제품과 일치하면 타 제품으로 재할당
            if curr_p.modality and _pkey(curr_p.modality) not in norm_txt:
                return p.product_id
            if p.name and _pkey(p.name) in norm_txt and _pkey(curr_p.name) not in norm_txt:
                return p.product_id
    return pid


def _procedure_from_number(rn: str) -> Optional[str]:
    m = re.search(r"[제수]\s*(허|인|신)", rn or "")
    return {"허": "approval", "인": "certification", "신": "notification"}.get(m.group(1)) if m else None


def _mentions_target(ctx: ClinicalRun, pid: str, statement: str, excerpt: Optional[str], source_ids: list[str],
                     allow_official: bool = False) -> bool:
    """근거가 이 회사·제품을 실제로 언급하는지 (일반 제도 설명 페이지를 근거로 쓰는 것 방지)"""
    if allow_official and any(ctx.sources.get(s, {}).get("publisher") == "식품의약품안전처" for s in source_ids):
        return True  # 업체명 필터를 거친 식약처 API 결과
    raw = " ".join([ctx.sources.get(s, {}).get("title", "") + " " + ctx.source_text.get(s, "") for s in source_ids])
    if excerpt:
        raw += " " + excerpt
    text = _norm_txt(raw).lower()
    keys = [_company_core(ctx.company.display_name), _company_core(ctx.company.legal_name), _company_core(ctx.company.english_name)]
    keys += [_company_core(a) for a in ctx.company.aliases]
    keys += [_norm_txt(n).lower() for n in ctx.product_names(pid)]
    return any(k and len(k) >= 2 and k in text for k in keys)


def _record_key(r: dict) -> tuple:
    return (r["product_id"], r["country"], r.get("record_number") or f'{r.get("authority")}|{r.get("procedure_type")}')


def regulatory_node(state: ClinicalAgentState) -> dict:
    """2. 공식 상태: 제품·국가별 기관 기록"""
    ctx = state["ctx"]
    if "regulatory" not in ctx.steps:
        return {}
    prev_records = [r for r in ctx.reg_records if r["product_id"] in ctx.target_pids]
    ctx.reg_records = [r for r in ctx.reg_records if r["product_id"] not in ctx.target_pids]

    # 비대상으로 분류됐어도 식약처 품목허가에 같은 품목이 있으면 규제 대상 (예: 초단파자극기, 보행분석계)
    match_names = _dedupe([ctx.company.legal_name, ctx.company.display_name, *ctx.company.aliases])
    company_items: list[dict] = []
    for n in ctx.company_names()[:2]:
        company_items += mfds_query(ctx, MFDS_PERMIT, MFDS_PERMIT["company_param"], n, match=match_names).get("items", [])
    item_names = [_pkey(v) for it in company_items for k, v in it.items()
                  if isinstance(v, str) and re.search(r"prdlst|prduct|mdl|품목|모델", k, re.I)]
    for pid in ctx.target_pids:
        if ctx.classify[pid]["regulatory_applicability"] == "out_of_scope":
            key = _pkey(ctx.product(pid).name)
            if key and any(key == n or (len(key) >= 4 and (key in n or n in key)) for n in item_names if n):
                ctx.classify[pid] = {**ctx.classify[pid], "regulatory_applicability": "in_scope",
                                     "rationale": "식약처 품목허가 기록이 있어 규제 대상으로 재판정"}

    groups: dict[tuple, list[dict]] = {}
    for pid in ctx.target_pids:
        appl = ctx.classify[pid]["regulatory_applicability"]
        for country in ctx.product(pid).target_countries:
            if appl == "out_of_scope":
                continue
            hits: list[dict] = []
            names = ctx.company_names()
            if country == "KR":
                found = None
                match = _dedupe([ctx.company.legal_name, ctx.company.display_name, *ctx.company.aliases])
                for n in names:
                    h = mfds_query(ctx, MFDS_PERMIT, MFDS_PERMIT["company_param"], n, match=match)
                    hits.append(h)
                    if h.get("items"):
                        found = n
                        break
                for pn in ctx.product_names(pid):
                    hits.append(mfds_query(ctx, MFDS_PERMIT, MFDS_PERMIT["product_param"], pn, match=match))
                # 품목허가 결과의 품목명을 품목정보 API에 넣어 사용 목적·취소 이력 조회 (허가 → 상세 연결)
                permit_items = [it for h in hits if h["tool"] == MFDS_PERMIT["name"] for it in h.get("items", [])]
                item_names = _item_values(permit_items, MFDS_ITEM["product_param"])
                for nm in item_names[:MFDS_ITEM_MAX_PER_COMPANY]:
                    hits.append(mfds_query(ctx, MFDS_ITEM, MFDS_ITEM["product_param"], nm, match=match))
                if not item_names:  # 품목명을 못 얻으면 업체명으로 시도 (변수가 설정된 경우)
                    for n in ([found] if found else names[:2]):
                        h = mfds_query(ctx, MFDS_ITEM, MFDS_ITEM["company_param"], n, match=match)
                        hits.append(h)
                        if h.get("items"):
                            break
                pn = (ctx.product_names(pid) or [""])[0]
                hits.append(web_search(ctx, f"{ctx.company.display_name} {pn} 식약처 허가 인증".strip()))
            elif country == "US":
                for n in _dedupe([ctx.company.english_name, ctx.company.legal_name])[:2]:
                    hits.append(openfda_510k(ctx, n))
                pn = (ctx.product_names(pid) or [ctx.company.display_name])[0]
                hits.append(web_search(ctx, f"{pn} FDA 510(k) clearance"))
            else:
                pn = (ctx.product_names(pid) or [ctx.company.display_name])[0]
                hits.append(web_search(ctx, f"{pn} {country} medical device regulatory approval"))
            groups[(pid, country)] = hits

    drafts: list[S.RegRecordDraft] = []
    if groups and any(h["ok"] for hs in groups.values() for h in hs):
        try:
            out: S.RegulatoryExtraction = ask(S.RegulatoryExtraction, "regulatory", {
                "기업": _company_brief(ctx),
                "제품·국가별 원자료": [{"product": _product_brief(ctx, pid), "country": c, "hits": hs}
                                  for (pid, c), hs in groups.items()],
            })
            drafts = out.records
        except Exception as e:
            ctx.validation_issues.append(f"허가 기록 추출 실패: {e}"[:200])

    found_keys: set[tuple] = set()
    seen_numbers: set[tuple] = set()
    for d in drafts:
        if d.status in ("not_found", "not_applicable", "unknown"):
            continue
        source_titles = [ctx.sources.get(s, {}).get("title", "") for s in d.source_ids]
        d.product_id = _reassign_product(ctx, d.product_id, d.statement, d.excerpt, d.permitted_use, *source_titles)
        permit_info = f"{d.permitted_use or ''} {d.statement or ''} {' '.join(source_titles)}"
        if _is_mismatched_item(ctx.product(d.product_id), permit_info):
            reassigned = False
            for op in ctx.company.products:
                if op.product_id != d.product_id and not _is_mismatched_item(op, permit_info):
                    d.product_id = op.product_id
                    reassigned = True
                    break
            if not reassigned:
                ctx.validation_issues.append(f"미매핑 허가 기록: {d.record_number or d.procedure_type} ({d.permitted_use or '품목'}) - 제품({ctx.product(d.product_id).name})과 품목/용도 불일치로 제외")
                continue

        if (d.product_id, d.country) not in groups:
            continue
        if not _mentions_target(ctx, d.product_id, d.statement, d.excerpt, d.source_ids, allow_official=True):
            continue  # 해당 회사·제품을 언급하지 않는 일반 자료는 근거로 쓰지 않음
        rn = _clean(d.record_number)
        official = any(ctx.sources.get(x, {}).get("publisher") == "식품의약품안전처" for x in d.source_ids)
        if rn and not official:
            rn = None  # H) 허가번호는 식약처 원자료에서만 인정 (기사 속 '제40호' 등으로 번호를 만들지 않음)

        # 지정은 허가가 아님: 허가번호가 없고 공식 기관(식약처/FDA) 출처가 아니며, 단순 기사이거나 지정에 관한 내용인 경우 허가 기록에서 제외
        is_designation_text = bool(re.search(r"혁신의료|지정|혁신제품|선정", (d.statement or "") + " " + (d.excerpt or "")))
        if not rn and not official and (is_designation_text or d.procedure_type in ("other", "unknown")):
            continue

        if rn:
            rn = re.sub(r"\s*호$", "", rn.strip())
            rn = re.sub(r"^(체외\s*)?(허|인|신)\s", lambda m: f"{m.group(1) or ''}제{m.group(2)} ", rn)
            proc = _procedure_from_number(rn)
            if proc and d.country == "KR":
                d.procedure_type = proc  # 제허=허가, 제인=인증, 제신=신고 (수입은 수허·수인·수신)
            if (d.country, rn) in seen_numbers:
                continue  # 허가번호 하나는 한 제품에만 속함 (제품 간 중복 제거)
            seen_numbers.add((d.country, rn))
            if proc and d.status == "pending":
                d.status = "active"  # 허가·인증·신고 번호가 발급됐으면 심사 중일 수 없음
        d.record_number = rn
        pu = _clean(d.permitted_use)
        d.permitted_use = None if (not pu or len(pu.strip()) <= 3 or pu.strip().isdigit()) else pu
        if len((d.statement or "").strip()) < 6:
            pname = ctx.product(d.product_id).name or d.product_id
            d.statement = f"{ctx.company.display_name} {pname} 식약처 {d.procedure_type} 기록" + (f" ({rn})" if rn else "")
        vu = strip_fake_jan1(norm_date(d.valid_until), (d.statement or "") + " " + (d.excerpt or ""))
        if vu and d.status == "active" and compare_to_as_of(vu, ctx.as_of) == "before":
            d.valid_until = None  # 갱신일 등 다른 날짜를 유효기간으로 잘못 읽은 경우로 보고 비움
        eff = strip_fake_jan1(norm_date(d.effective_at), (d.statement or "") + " " + (d.excerpt or ""))
        if compare_to_as_of(eff, ctx.as_of) == "after":
            continue  # 기준일 당시 존재하지 않던 기록
        eid = ctx.add_evidence(d.product_id, d.statement, d.source_ids, d.evidence_status, d.excerpt,
                               eff, d.country)
        if not eid:
            continue  # 출처가 확인되지 않은 기록은 버림
        rec = {"product_id": d.product_id, "country": d.country, "authority": d.authority,
               "procedure_type": d.procedure_type, "record_number": d.record_number,
               "permitted_use": d.permitted_use, "status": d.status, "effective_at": eff,
               "valid_until": norm_date(d.valid_until), "evidence_ids": [eid]}
        rid = ctx.reuse_id(prev_records, "record_id", _record_key, _record_key(rec)) or ctx.new_id("reg")
        ctx.reg_records.append(S.RegulatoryRecord(record_id=rid, **rec).model_dump())
        found_keys.add((d.product_id, d.country))

    authority = {"KR": "식품의약품안전처", "US": "U.S. FDA"}
    for pid in ctx.target_pids:
        appl = ctx.classify[pid]["regulatory_applicability"]
        for country in ctx.product(pid).target_countries:
            if appl == "out_of_scope":
                rec = {"product_id": pid, "country": country, "authority": "해당 없음", "procedure_type": "unknown",
                       "status": "not_applicable", "evidence_ids": []}
                rid = ctx.reuse_id(prev_records, "record_id", _record_key, _record_key(rec)) or ctx.new_id("reg")
                ctx.reg_records.append(S.RegulatoryRecord(record_id=rid, **rec).model_dump())
                ctx.set_coverage("regulatory", pid, country, "reviewed", note="규제 비대상으로 조회하지 않음")
                continue
            hits = groups.get((pid, country), [])
            found = (pid, country) in found_keys
            status = _coverage_status(hits, found)
            if not found:
                rec_status = "not_found" if any(h["ok"] for h in hits) else "unknown"
                rec = {"product_id": pid, "country": country, "authority": authority.get(country, "미상"),
                       "procedure_type": "unknown",
                       "status": rec_status,
                       "evidence_ids": []}
                rid = ctx.reuse_id(prev_records, "record_id", _record_key, _record_key(rec)) or ctx.new_id("reg")
                ctx.reg_records.append(S.RegulatoryRecord(record_id=rid, **rec).model_dump())
                ctx.set_coverage("regulatory", pid, country, "reviewed" if rec_status == "not_found" else status, **_scope_of(hits),
                                 note="조회 범위 내 미발견 (무허가를 뜻하지 않음)" if rec_status == "not_found" else "조회 실패로 상태 불명")
            else:
                ctx.set_coverage("regulatory", pid, country, status, **_scope_of(hits))
    return {}
