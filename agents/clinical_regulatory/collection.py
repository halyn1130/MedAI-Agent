"""
임상·인허가 자료 수집 (평가 노드 밖: 초기 데이터 로더·수집 노드에서 호출)

평가 노드(agent.clinical_analysis_node)는 외부 조회를 하지 않는다.
이 파일은 공통 데이터셋 계약(Source · DataRecord) 형식으로 임상·인허가 자료를 만들어 준다.

    out = collect_clinical_data(company_id, company_profile, as_of)
    state["company_profile"] = out["company_profile"]   # 정식명·제품·회사 주장 원문이 채워진 프로필
    state["dataset"] = out["dataset"]                   # {version, sources, records} (다른 자료와 합쳐도 됨)

Source     : source_id, title, url, published_at, collected_at (+ publisher, source_type)
DataRecord : record_id, source_id, content, locator, tags

※ 수집 노드로 옮기거나 다른 자료와 합칠 때 아래 두 가지는 그대로 유지해 주세요.
  1) DataRecord.tags : 평가 노드가 이 태그로 단계·제품·국가별 자료를 고른다.
     지우면 모든 자료가 모든 단계에 들어가 LLM 입력이 잘리고, 다른 Worker 이름만 남기면 임상 평가에서 빠진다.
  2) Source.publisher · source_type : 공통 계약에 없는 부가 필드지만 식약처 공식 기록 판별에 쓴다.
     (publisher · tool:mfds_* 태그 · 식약처 URL 중 하나는 남아 있어야 허가번호가 인정된다)

tags (평가 노드가 자료를 배분할 때 읽는다)
- clinical                          : 임상·인허가 Worker용
- area:regulatory | area:institutional | area:studies | area:profile
- product:<product_id> · country:<국가코드>   : 없으면 모든 제품·국가에 쓴다
- tool:mfds_permit | tool:mfds_item | tool:openfda | tool:pubmed | tool:web | tool:page
  (tool:mfds_* 는 식약처 공식 기록. content는 품목 목록 JSON)

단계별 함수(collect_profile, collect_regulatory, collect_institutional, collect_studies)는 따로 호출할 수 있다.
단독 실행: python collection.py startup_list.csv 기업명
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import urllib.parse
from typing import Optional

try:
    from . import schema as S
    from .config import (
        AGENT,
        COLLECT_MAX_PAGES,
        DEFAULT_TARGET_COUNTRIES,
        MFDS_ITEM,
        MFDS_ITEM_MAX_PER_COMPANY,
        MFDS_PERMIT,
    )
    from .context import normalize_profile
    from .llm import ask
    from .tools import CollectRun, _item_values, fetch_page, mfds_query, openfda_510k, pubmed_search, web_search
    from .utils import (
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
        today,
    )
except ImportError:
    import schema as S
    from config import (
        AGENT,
        COLLECT_MAX_PAGES,
        DEFAULT_TARGET_COUNTRIES,
        MFDS_ITEM,
        MFDS_ITEM_MAX_PER_COMPANY,
        MFDS_PERMIT,
    )
    from context import normalize_profile
    from llm import ask
    from tools import CollectRun, _item_values, fetch_page, mfds_query, openfda_510k, pubmed_search, web_search
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
        today,
    )


# ══════════════════════════════════════════════
# 조회 결과 → DataRecord
# ══════════════════════════════════════════════
def _add_record(ctx: CollectRun, source_id: Optional[str], content: str, tags: list[str],
                locator: Optional[str] = None) -> None:
    """같은 출처·같은 내용은 레코드 하나로 두고 tags만 합친다 (레코드 ID의 내용은 고정)"""
    if not source_id:
        return
    rec = ctx.records.get((source_id, content))
    if rec is None:
        rec = ctx.records[(source_id, content)] = S.DataRecord(
            record_id=ctx.new_id("rec"), source_id=source_id, content=content, locator=locator).model_dump()
    rec["tags"] = _dedupe(rec["tags"] + tags)


def _add_hits(ctx: CollectRun, hits: list[dict], area: str, pid: Optional[str] = None,
              country: Optional[str] = None) -> None:
    # 평가 노드(context.ClinicalRun.records_for)가 읽는 태그. 수집 노드에서도 그대로 유지할 것
    tags = [AGENT, f"area:{area}"] + ([f"product:{pid}"] if pid else []) + ([f"country:{country}"] if country else [])
    for h in hits:
        if not h["ok"]:
            continue
        tool = h["tool"]
        if tool.startswith("식약처"):
            if h.get("items"):
                kind = "tool:mfds_permit" if tool == MFDS_PERMIT["name"] else "tool:mfds_item"
                _add_record(ctx, h.get("source_id"), json.dumps(h["items"], ensure_ascii=False), tags + [kind],
                            locator=f"조회어: {h['query']}")
        elif tool == "웹 검색":
            for r in h["results"]:
                _add_record(ctx, r["source_id"], r.get("content") or "", tags + ["tool:web"])
        elif tool == "PubMed":
            for paper in h["papers"]:
                body = {k: v for k, v in paper.items() if k != "source_id"}
                _add_record(ctx, paper["source_id"], json.dumps(body, ensure_ascii=False), tags + ["tool:pubmed"])
        elif tool.startswith("openFDA"):
            for r in h["results"]:
                body = {k: v for k, v in r.items() if k != "source_id"}
                _add_record(ctx, r["source_id"], json.dumps(body, ensure_ascii=False), tags + ["tool:openfda"])
        elif tool == "페이지":
            _add_record(ctx, h.get("source_id"), h["text"], tags + ["tool:page"])


# ══════════════════════════════════════════════
# 0. 기업·제품 정보 · 회사 주장 원문
# ══════════════════════════════════════════════
def _collected_to_profile(ctx: CollectRun, prof: S.ProfileCollection, claims: list[dict]) -> dict:
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


def collect_profile(ctx: CollectRun) -> None:
    """정식명·홈페이지·제품·회사 주장 원문을 모아 ctx.company에 반영한다.
    정식명과 주장이 이미 입력에 있으면 수집하지 않는다."""
    c = ctx.company
    has_claims = any(p.claims for p in c.products)
    if c.legal_name and has_claims:
        return
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
    _add_hits(ctx, hits + pages, "profile")

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
            ctx.issues.append(f"주장 원문 수집 실패: {e}"[:200])

    # 5) 프로필 갱신
    collected = _collected_to_profile(ctx, prof, claims)
    if dropped:
        ctx.issues.append(f"본문에 그대로 없어 버린 주장 문장 {len(dropped)}건")
    ctx.company = normalize_profile(ctx.cid, {**ctx.raw_profile, **collected})


# ══════════════════════════════════════════════
# 2. 공식 상태 원자료 (제품·국가별)
# ══════════════════════════════════════════════
def collect_regulatory(ctx: CollectRun) -> None:
    """식약처 품목허가·품목정보, openFDA 510(k), 웹 검색"""
    match = _dedupe([ctx.company.legal_name, ctx.company.display_name, *ctx.company.aliases])
    names = ctx.company_names()
    # 업체명 품목허가 조회는 제품 구분 없이 둔다 (규제 대상 여부 재판정에 사용)
    _add_hits(ctx, [mfds_query(ctx, MFDS_PERMIT, MFDS_PERMIT["company_param"], n, match=match) for n in names[:2]],
              "regulatory")

    for p in ctx.company.products:
        pid = p.product_id
        for country in p.target_countries:
            hits: list[dict] = []
            if country == "KR":
                found = None
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
            _add_hits(ctx, hits, "regulatory", pid, country)


# ══════════════════════════════════════════════
# 3. 제도권 진입 원자료 (지정·평가·등재·지불 경로)
# ══════════════════════════════════════════════
def collect_institutional(ctx: CollectRun) -> None:
    for p in ctx.company.products:
        pid = p.product_id
        pn = (ctx.product_names(pid) or [ctx.company.display_name])[0]
        for country in p.target_countries:
            if country == "KR":
                queries = [f"{ctx.company.display_name} 혁신의료기기 지정", f"{pn} 혁신의료기술 평가",
                           f"{pn} 비급여 등재 고시", f"{ctx.company.display_name} 건강보험 급여 등재"]
            elif country == "US":
                queries = [f"{pn} FDA Breakthrough Device designation", f"{pn} CMS reimbursement"]
            else:
                queries = [f"{pn} {country} reimbursement"]
            _add_hits(ctx, [web_search(ctx, q) for q in queries], "institutional", pid, country)


# ══════════════════════════════════════════════
# 4. 연구 근거 원자료 (PubMed · 웹 검색)
# ══════════════════════════════════════════════
def collect_studies(ctx: CollectRun) -> None:
    for p in ctx.company.products:
        pid = p.product_id
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
        _add_hits(ctx, hits, "studies", pid)


# ══════════════════════════════════════════════
# 기업 1곳 수집
# ══════════════════════════════════════════════
def collect_clinical_data(company_id: str, company_profile: dict, as_of: Optional[str] = None) -> dict:
    as_of = as_of or today()
    ctx = CollectRun(normalize_profile(company_id, company_profile), as_of, company_profile)
    try:
        collect_profile(ctx)
    except Exception as e:  # 실패해도 입력 프로필로 나머지 수집을 계속한다
        ctx.issues.append(f"기업·제품 정보 수집 중 오류, 입력 프로필로 진행: {type(e).__name__}: {e}"[:200])
    collect_regulatory(ctx)
    collect_institutional(ctx)
    collect_studies(ctx)
    records = list(ctx.records.values())
    used = {r["source_id"] for r in records}
    digest = hashlib.sha1(json.dumps([[r["source_id"], r["content"]] for r in records],
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
    return {
        "company_profile": ctx.company.model_dump(),
        "dataset": {"version": f"clinical-{digest}",   # 내용이 바뀌면 값이 바뀐다
                    "sources": [s for sid, s in ctx.sources.items() if sid in used], "records": records},
        "issues": ctx.issues,   # 수집 중 문제 (로그용)
    }


if __name__ == "__main__":
    csv_path, name = sys.argv[1], sys.argv[2]
    with open(csv_path, encoding="utf-8-sig") as f:
        row = next(r for r in csv.DictReader(f) if (r.get("기업명") or "").strip() == name)
    print(json.dumps(collect_clinical_data("c001", row), ensure_ascii=False, indent=2))
