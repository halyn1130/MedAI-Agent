from __future__ import annotations

try:
    from .. import schema as S
    from ..context import ClinicalAgentState
    from ..llm import _company_brief, _product_brief, ask
except ImportError:
    import schema as S
    from context import ClinicalAgentState
    from llm import _company_brief, _product_brief, ask


def classify_node(state: ClinicalAgentState) -> dict:
    """1. 유형·범위: product_type과 regulatory_applicability를 분리"""
    ctx = state["ctx"]
    for p in ctx.company.products:  # 이전 판단이 없는 제품의 기본값
        ctx.classify.setdefault(p.product_id, {"product_type": "other", "regulatory_applicability": "unknown",
                                               "has_efficacy_claim": bool(p.claims), "rationale": "미분류"})
    if "classify" not in ctx.steps:
        return {}
    pids = ctx.target_pids
    try:
        out: S.ClassifyOutput = ask(S.ClassifyOutput, "classify", {
            "기업": _company_brief(ctx), "제품": [_product_brief(ctx, pid) for pid in pids]})
        for it in out.items:
            if it.product_id in pids:
                ctx.classify[it.product_id] = it.model_dump()

        # 규제 대상 보정: 인허가 주장 또는 식약처 품목에 명백히 해당하는 경우 in_scope 강제
        for pid in pids:
            item = ctx.classify.get(pid, {})
            p = ctx.product(pid)
            claim_text = " ".join(c.text for c in p.claims).lower()
            name_and_use = f"{p.name or ''} {p.intended_use or ''}".lower()
            has_reg_claim = any(kw in claim_text for kw in ("의료기기 신고", "의료기기 인증", "의료기기 허가", "ce mdr", "fda 승인", "fda 허가", "ce 인증"))
            has_device_kw = any(kw in name_and_use for kw in ("cstd", "폐쇄형 약물", "보행분석", "진료용 장갑", "초단파 자극", "인공와우", "체외진단"))
            if (has_reg_claim or has_device_kw) and item.get("regulatory_applicability") != "in_scope":
                item["regulatory_applicability"] = "in_scope"
                item["rationale"] = (item.get("rationale") or "") + " (의료기기 품목 또는 인허가 획득 주장에 따라 in_scope로 보정)"
                ctx.classify[pid] = item

        status = "reviewed"
    except Exception as e:
        ctx.validation_issues.append(f"유형 분류 실패: {e}"[:200])
        status = "search_failed"
    for pid in pids:
        p = ctx.product(pid)
        info_missing = not (p.name or p.intended_use or ctx.company.description)
        ctx.set_coverage("classification", pid, None, "insufficient_information" if info_missing else status,
                         note=ctx.classify[pid].get("rationale"))
    return {}
