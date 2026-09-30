from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

try:
    from .. import schema as S
    from ..config import COLLECT_MAX_PAGES, COLLECT_ON, DEFAULT_TARGET_COUNTRIES, MFDS_PERMIT
    from ..context import ClinicalAgentState, ClinicalRun, normalize_profile
    from ..llm import ask
    from ..tools import fetch_page, mfds_query, web_search
    from ..utils import (
        _clean,
        _company_core,
        _core_name,
        _dedupe,
        _is_generic_product,
        _is_product_claim,
        _mentioned_products,
        _merge_products,
        _norm_txt,
        _pkey,
        homepage_hint,
        norm_date,
        same_company,
        source_slug,
        strip_fake_jan1,
    )
except ImportError:
    import schema as S
    from config import COLLECT_MAX_PAGES, COLLECT_ON, DEFAULT_TARGET_COUNTRIES, MFDS_PERMIT
    from context import ClinicalAgentState, ClinicalRun, normalize_profile
    from llm import ask
    from tools import fetch_page, mfds_query, web_search
    from utils import (
        _clean,
        _company_core,
        _core_name,
        _dedupe,
        _is_generic_product,
        _is_product_claim,
        _mentioned_products,
        _merge_products,
        _norm_txt,
        _pkey,
        homepage_hint,
        norm_date,
        same_company,
        source_slug,
        strip_fake_jan1,
    )


def _collected_to_profile(ctx: ClinicalRun, prof: S.ProfileCollection, claims: list[dict]) -> dict:
    base = ctx.company
    real = [p for p in _merge_products(prof.products) if not _is_generic_product(p.name, base.display_name)]
    # CSV의 주요 제품이 real에 포함되지 않은 경우(예: 1등급 소모품만 등록된 토닥 '인공와우' 등),
    # CSV의 대표 제품을 보존하여 고등급 허가/연구 매칭이 가능하도록 한다.
    for bp in base.products:
        if bp.name and not _is_generic_product(bp.name, base.display_name):
            bp_k = _pkey(bp.name)
            if not any(bp_k in _pkey(rp.name) or _pkey(rp.name) in bp_k for rp in real):
                real.insert(0, S.CollectedProduct(name=bp.name, intended_use=bp.intended_use or bp.name))

    src_products = real or [S.CollectedProduct(name=p.name or base.display_name) for p in base.products]
    claims = [dict(c, text=re.sub(r"\s+", " ", c["text"]).strip()) for c in claims if _is_product_claim(c["text"])]
    assigned: list[list[dict]] = [[] for _ in src_products]
    seen_text: set[str] = set()
    for c in claims:
        key = _norm_txt(c["text"])
        if key in seen_text:
            continue  # 같은 문장은 한 번만
        seen_text.add(key)
        idx = _mentioned_products(c["text"], src_products)
        if not idx and c.get("product_name"):  # 문장에 제품명이 없으면 LLM이 지정한 제품
            idx = [i for i, p in enumerate(src_products) if _pkey(p.name) == _pkey(c["product_name"])]
        if not idx:
            idx = [0]  # 회사 전체에 대한 문장은 대표 제품 하나에만 연결 (제품마다 복제하지 않음)
        for i in idx:
            assigned[i].append(c)
    products = []
    for i, p in enumerate(src_products, 1):
        products.append({"product_id": f"p{i:03d}", **{k: _clean(v) for k, v in p.model_dump().items()},
                         "target_countries": list(DEFAULT_TARGET_COUNTRIES),
                         "claims": [{"text": c["text"], "url": c["url"], "claimed_at": c.get("claimed_at")}
                                    for c in assigned[i - 1]]})
    return {"legal_name": _clean(prof.legal_name), "english_name": _clean(prof.english_name), "homepage": _clean(prof.homepage),
            "aliases": prof.aliases, "products": products}


def _collect(ctx: ClinicalRun) -> dict:
    c = ctx.company
    has_claims = any(p.claims for p in c.products)
    if not COLLECT_ON or ctx.mode != "initial" or (c.legal_name and has_claims):
        return {}
    ctx.self_collected = True
    main_product = (c.products[0].name if c.products else "") or ""

    # 1) 식약처 품목허가: 정식 업체명·허가 품목 후보
    hits: list[dict] = []
    for n in _dedupe([c.display_name, f"주식회사 {_core_name(c.display_name)}"]):
        h = mfds_query(ctx, MFDS_PERMIT, MFDS_PERMIT["company_param"], n, match=[c.display_name])
        hits.append(h)
        if h.get("items"):
            break
    # 2) 웹 검색: 홈페이지·제품·주장이 있을 페이지 후보
    category = ctx.raw_profile.get("대분류") or ""
    queries = [f"{c.display_name} {main_product}".strip(), f"{c.display_name} 공식 홈페이지",
               f"{c.display_name} {main_product} 정확도 허가 보도자료".strip()]
    queries.append(f"{c.display_name} 주식회사 설립 대표 {category}".strip())  # 기업 개요 페이지(법인명·영문명) 유도
    if len(_core_name(c.display_name)) <= 3:  # 짧은 이름(닷·아크·티알 등)은 분야를 붙여 동명이인 노이즈 감소
        queries.append(f"{c.display_name} {category} 스타트업 대표".strip())
    slug = source_slug(ctx.raw_profile)
    if slug:  # 출처 URL의 영문 식별자로 영문 자료 검색 (한글명이 모호할 때 효과적)
        queries.append(f"{slug} {main_product}".strip())
        queries.append(f"{slug} company Korea")
    for q in queries:
        hits.append(web_search(ctx, q))
    allowed_urls = {r["url"] for h in hits for r in h.get("results", [])}
    if c.homepage:  # CSV에 홈페이지를 직접 적어두면 그대로 사용
        allowed_urls.add(c.homepage)
    home_hint = homepage_hint(ctx.raw_profile)  # 출처 URL이 회사 홈페이지인 경우 (예: aiark.io)
    if home_hint:
        allowed_urls.add(home_hint)

    prof: S.ProfileCollection = ask(S.ProfileCollection, "collect_profile", {
        "CSV 입력": ctx.raw_profile,
        "검색 단서 (사실 아님)": {"출처 URL 영문 식별자": slug},
        "원자료": hits})
    if prof is None:
        raise ValueError("수집 결과 없음")

    # CSV에 직접 적은 값이 있으면 우선
    prof.homepage = c.homepage or prof.homepage or home_hint
    prof.legal_name = c.legal_name or prof.legal_name
    # 원자료에 없는 URL은 버림 (추측 방지)
    if prof.homepage and prof.homepage not in allowed_urls:
        root = {f"{urllib.parse.urlparse(u).scheme}://{urllib.parse.urlparse(u).netloc}" for u in allowed_urls}
        prof.homepage = prof.homepage if prof.homepage.rstrip("/") in root else None
    urls = _dedupe([prof.homepage] + [u for u in prof.extract_urls if u in allowed_urls])[:COLLECT_MAX_PAGES]

    # 3) 페이지 본문 수집 (홈페이지 먼저 반영해야 출처 유형이 company로 분류됨)
    ctx.company.homepage = prof.homepage
    pages = [p for p in (fetch_page(ctx, u) for u in urls) if p["ok"]]

    # 정식명은 원자료·본문에 실제로 있어야 인정
    corpus = _norm_txt(json.dumps(hits, ensure_ascii=False) + " ".join(p["text"] for p in pages))
    if prof.legal_name and not c.legal_name:
        company_pages = "".join(_norm_txt(p["text"]) for p in pages
                                if ctx.sources.get(p.get("source_id") or "", {}).get("source_type") == "company")
        # 이름이 같거나(법인 표기만 다름) 회사 홈페이지 본문에 있을 때만 인정 → 다른 회사 오인 방지
        if not (same_company(prof.legal_name, c.display_name) or
                (_norm_txt(prof.legal_name) in company_pages and _company_core(c.display_name) in _company_core(prof.legal_name))):
            prof.legal_name = None

    # 4) 회사 주장 원문 추출 → 본문에 그대로 있는 문장만 채택
    claims: list[dict] = []
    dropped: list[str] = []
    if pages:
        try:
            out: S.ClaimCollection = ask(S.ClaimCollection, "collect_claims", {
                "기업": {"display_name": c.display_name, "legal_name": prof.legal_name},
                "제품": [p.model_dump() for p in prof.products],
                "페이지 본문": [{"url": p["query"], "text": p["text"][:8000]} for p in pages],
            })
            page_text = {p["query"]: _norm_txt(p["text"]) for p in pages}
            all_text = "".join(page_text.values())
            company_urls = {p["query"] for p in pages
                            if ctx.sources.get(p.get("source_id") or "", {}).get("source_type") == "company"}
            name_keys = [k for k in [_company_core(c.display_name)] + [_pkey(x.name) for x in prof.products] if k and len(k) >= 2]
            for cl in out.claims:
                t = _norm_txt(cl.text)
                about_company = (
                    cl.url in company_urls
                    or any(k in _pkey(cl.text) for k in name_keys)
                    or any(k in _pkey(cl.product_name or "") for k in name_keys)
                )
                if not about_company:
                    dropped.append(cl.text[:80])  # 기사 속 업계 일반 설명 등
                    continue
                if len(t) >= 5 and (t in page_text.get(cl.url, "") or t in all_text):
                    url = cl.url if cl.url in page_text else next(u for u, v in page_text.items() if t in v)
                    c_date = strip_fake_jan1(norm_date(cl.claimed_at), cl.text)
                    claims.append({"product_name": cl.product_name, "text": cl.text.strip(), "url": url,
                                   "claimed_at": c_date})
                else:
                    dropped.append(cl.text[:80])
        except Exception as e:
            ctx.validation_issues.append(f"주장 원문 수집 실패: {e}"[:200])

    # 5) 프로필 갱신
    ctx.collected = _collected_to_profile(ctx, prof, claims)
    ctx.collected["collection_notes"] = {
        "searched_pages": [p["query"] for p in pages],
        "dropped_claims_not_in_page": dropped,   # 본문에 그대로 없어 버린 문장
        "collected_at": ctx.checked_at,
    }
    ctx.company = normalize_profile(ctx.cid, {**ctx.raw_profile, **ctx.collected})
    return {}


def collect_node(state: ClinicalAgentState) -> dict:
    """0. 자체 수집: CSV만 입력된 경우 정식명·홈페이지·제품·회사 주장 원문을 직접 모은다.
    수집이 실패해도 CSV 정보로 분석을 계속한다."""
    ctx = state["ctx"]
    try:
        return _collect(ctx)
    except Exception as e:
        ctx.validation_issues.append(f"자체 수집 중 오류, CSV 정보로 진행: {type(e).__name__}: {e}"[:200])
        return {}
