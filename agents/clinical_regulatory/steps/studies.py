from __future__ import annotations

try:
    from .. import schema as S
    from ..config import LEVEL_RANK
    from ..context import ClinicalRun
    from ..llm import _company_brief, _product_brief, ask
    from ..utils import _pkey, compare_to_as_of, norm_date, strip_fake_jan1
except ImportError:
    import schema as S
    from config import LEVEL_RANK
    from context import ClinicalRun
    from llm import _company_brief, _product_brief, ask
    from utils import _pkey, compare_to_as_of, norm_date, strip_fake_jan1


def studies_step(ctx: ClinicalRun) -> None:
    """4. 연구 근거: 비규제 제품도 효능 주장이 있으면 수행"""
    groups: dict[str, list[dict]] = {}
    for pid in ctx.pids:
        info = ctx.classify[pid]
        has_claim = info.get("has_efficacy_claim")
        has_claim = bool(ctx.product(pid).claims) if has_claim is None else has_claim
        if info["regulatory_applicability"] == "out_of_scope" and not has_claim:
            ctx.set_coverage("clinical_studies", pid, None, "not_applicable", note="규제 비대상이며 효능 주장 없음")
            continue
        groups[pid] = ctx.records_for("studies", pid)

    drafts: list[S.StudyDraft] = []
    failed = False
    if any(groups.values()):
        try:
            out: S.StudyExtraction = ask(S.StudyExtraction, "studies", {
                "기업": _company_brief(ctx),
                "제품별 원자료": [{"product": _product_brief(ctx, pid), "records": [ctx.record_view(r) for r in rs]}
                             for pid, rs in groups.items() if rs],
                **ctx.review_payload(),
            })
            drafts = out.studies
        except Exception as e:
            ctx.add_error("studies_extraction_failed", e)
            failed = True

    found: set[str] = set()
    for d in drafts:
        if d.product_id not in groups or d.evidence_level == "L0":
            continue
        if not d.product_match:
            continue  # 이름만 같은 다른 연구(예: LUCAS 임상시험)는 제품 근거가 아니므로 기록하지 않음
        titles = " ".join(ctx.record_title(r) for r in d.record_ids)
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
        eids = ctx.add_evidence(d.product_id, d.statement, d.record_ids, d.evidence_status, d.excerpt, ev_date)
        if not eids:
            continue
        study = d.model_dump(exclude={"statement", "excerpt", "event_date", "record_ids", "evidence_status"})
        study["evidence_ids"] = eids
        ctx.studies.append(S.ClinicalStudy(study_id=ctx.new_id("st"), **study).model_dump())
        found.add(d.product_id)

    for pid, records in groups.items():
        if not records:
            ctx.set_coverage("clinical_studies", pid, None, "not_reviewed", note="제공된 자료 없음")
        elif failed:
            ctx.set_coverage("clinical_studies", pid, None, "partial", records, note="자료 해석 실패")
        else:
            ctx.set_coverage("clinical_studies", pid, None, "reviewed", records,
                             note=None if pid in found else "제공 자료에서 제품 관련 연구 미발견")
