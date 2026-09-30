from __future__ import annotations

import re

try:
    from .. import schema as S
    from ..config import LEVEL_RANK
    from ..context import ClinicalAgentState, ClinicalRun
    from ..llm import _company_brief, _product_brief, ask
    from ..tools import _coverage_status, _scope_of, pubmed_search, web_search
    from ..utils import _company_core, _dedupe, _pkey, compare_to_as_of, norm_date, strip_fake_jan1
except ImportError:
    import schema as S
    from config import LEVEL_RANK
    from context import ClinicalAgentState, ClinicalRun
    from llm import _company_brief, _product_brief, ask
    from tools import _coverage_status, _scope_of, pubmed_search, web_search
    from utils import _company_core, _dedupe, _pkey, compare_to_as_of, norm_date, strip_fake_jan1


def _study_key(s: dict, ctx: ClinicalRun) -> tuple:
    urls = sorted(ctx.sources[sid]["url"] for eid in s["evidence_ids"] if eid in ctx.evidence
                  for sid in ctx.evidence[eid]["source_ids"] if sid in ctx.sources)
    return (s["product_id"], tuple(urls), s["design"])


def studies_node(state: ClinicalAgentState) -> dict:
    """4. 연구 근거: 비규제 제품도 효능 주장이 있으면 수행"""
    ctx = state["ctx"]
    if "studies" not in ctx.steps:
        return {}
    prev_studies = [s for s in ctx.studies if s["product_id"] in ctx.target_pids]
    ctx.studies = [s for s in ctx.studies if s["product_id"] not in ctx.target_pids]

    groups: dict[str, list[dict]] = {}
    for pid in ctx.target_pids:
        info = ctx.classify[pid]
        has_claim = info.get("has_efficacy_claim")
        has_claim = bool(ctx.product(pid).claims) if has_claim is None else has_claim
        if info["regulatory_applicability"] == "out_of_scope" and not has_claim:
            ctx.set_coverage("clinical_studies", pid, None, "reviewed", note="규제 비대상이며 효능 주장 없음")
            continue
        hits: list[dict] = []
        for pn in [n for n in ctx.product_names(pid) if n.isascii() and re.search(r"[A-Za-z]", n)][:3]:
            hits.append(pubmed_search(ctx, f'"{pn}"[Title/Abstract]'))  # PubMed는 영문 검색만 의미 있음
        eng_affils = []
        if ctx.company.english_name:
            eng_affils.append(ctx.company.english_name)
            core = _company_core(ctx.company.english_name)
            if core and len(core) >= 3:
                eng_affils.append(core)
        for ea in _dedupe(eng_affils)[:2]:
            hits.append(pubmed_search(ctx, f'"{ea}"[Affiliation]'))
        pn = (ctx.product_names(pid) or [ctx.company.display_name])[0]
        hits.append(web_search(ctx, f"{ctx.company.display_name} {pn} 임상 연구 논문"))
        hits.append(web_search(ctx, f"{pn} clinical validation study"))
        groups[pid] = hits

    drafts: list[S.StudyDraft] = []
    if groups and any(h["ok"] for hs in groups.values() for h in hs):
        try:
            out: S.StudyExtraction = ask(S.StudyExtraction, "studies", {
                "기업": _company_brief(ctx),
                "제품별 원자료": [{"product": _product_brief(ctx, pid), "hits": hs} for pid, hs in groups.items()],
            })
            drafts = out.studies
        except Exception as e:
            ctx.validation_issues.append(f"연구 추출 실패: {e}"[:200])

    found: set[str] = set()
    for d in drafts:
        if d.product_id not in groups or d.evidence_level == "L0":
            continue
        if not d.product_match:
            continue  # 이름만 같은 다른 연구(예: LUCAS 임상시험)는 제품 근거가 아니므로 기록하지 않음
        titles = " ".join(ctx.sources.get(x, {}).get("title", "") for x in d.source_ids)
        named = any(_pkey(n) and _pkey(n) in _pkey(titles + " " + (d.statement or "")) for n in ctx.product_names(d.product_id))
        if not named and LEVEL_RANK.get(d.evidence_level, 0) > LEVEL_RANK["L2"]:
            d.evidence_level = "L2"  # 제품명이 없는 회사 연구는 간접 근거라 최대 L2
            d.limitations = ((d.limitations or "") + " / 제품명 미기재 간접 근거로 L2 상한 적용").strip(" /")

        # L3 요건: 전향적 다기관(prospective and multicenter) 또는 독립 외부 검증(external_validation)
        if d.evidence_level == "L3":
            is_prospective_multi = (d.prospective is True and d.multicenter is True)
            is_ext_val = (d.external_validation is True)
            if not (is_prospective_multi or is_ext_val):
                d.evidence_level = "L2"
                d.limitations = ((d.limitations or "") + " / 전향적 다기관 또는 외부 검증 미충족으로 L2 상한 적용").strip(" /")

        # 결과 미발표/진행 중인 연구는 result_direction을 unclear로 변경
        unreported_kws = ("진행 중", "예정", "모집 중", "모집 완료", "미발표", "발표되지 않", "결과 없음",
                          "ongoing", "recruiting", "not yet published", "planned", "protocol")
        res_text = ((d.result or "") + " " + (d.endpoint or "") + " " + (d.statement or "")).lower()
        if any(kw in res_text for kw in unreported_kws):
            d.result_direction = "unclear"
            d.limitations = ((d.limitations or "") + " / 임상 결과 미발표 또는 진행 중(점수 제외)").strip(" /")

        ev_date = strip_fake_jan1(norm_date(d.event_date), titles + " " + (d.statement or ""))
        if compare_to_as_of(ev_date, ctx.as_of) == "after":
            continue
        eid = ctx.add_evidence(d.product_id, d.statement, d.source_ids, d.evidence_status, d.excerpt, ev_date)
        if not eid:
            continue
        study = d.model_dump(exclude={"statement", "excerpt", "event_date", "source_ids", "evidence_status"})
        study["evidence_ids"] = [eid]
        sid = ctx.reuse_id(prev_studies, "study_id", lambda s: _study_key(s, ctx),
                           _study_key({**study}, ctx)) or ctx.new_id("st")
        ctx.studies.append(S.ClinicalStudy(study_id=sid, **study).model_dump())
        found.add(d.product_id)

    for pid, hits in groups.items():
        ctx.set_coverage("clinical_studies", pid, None, _coverage_status(hits, pid in found), **_scope_of(hits))
    return {}
