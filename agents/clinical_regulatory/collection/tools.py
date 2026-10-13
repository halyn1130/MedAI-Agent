from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Optional

import requests

try:
    from .. import schema as S
    from ..config import HTTP_TIMEOUT, MFDS_API_KEY, TRANSIENT_RETRY, WEB_RETRY, _CALL_SEM
    from ..context import CompanyView
    from ..utils import (
        _cache_get,
        _cache_put,
        _dedupe,
        _item_companies,
        _publisher_of,
        _Transient,
        compare_to_as_of,
        date_from_url,
        norm_date,
        same_company,
        source_type_of,
    )
except ImportError:
    import schema as S
    from config import HTTP_TIMEOUT, MFDS_API_KEY, TRANSIENT_RETRY, WEB_RETRY, _CALL_SEM
    from context import CompanyView
    from utils import (
        _cache_get,
        _cache_put,
        _dedupe,
        _item_companies,
        _publisher_of,
        _Transient,
        compare_to_as_of,
        date_from_url,
        norm_date,
        same_company,
        source_type_of,
    )


# ══════════════════════════════════════════════
# 기업 1곳 수집 컨텍스트 (출처 등록·조회 결과 재사용)
# ══════════════════════════════════════════════
class CollectRun(CompanyView):
    def __init__(self, company: S.CompanyInput, as_of: str, raw_profile: dict):
        super().__init__(company, as_of)
        self.raw_profile = raw_profile
        self.url_to_sid: dict[str, str] = {}
        self.memo: dict[tuple, dict] = {}
        self.records: dict[tuple, dict] = {}   # (source_id, content) → DataRecord
        self.issues: list[str] = []

    def add_source(self, title: str, url: str, publisher: str, source_type: str,
                   published_at: Optional[str] = None) -> Optional[str]:
        """기준일 이후 공개 자료는 등록하지 않고 None 반환"""
        pub = norm_date(published_at)
        rel = compare_to_as_of(pub, self.as_of)
        if rel == "after":
            return None
        if rel == "ambiguous":
            pub = None  # 기준일 당시 존재를 확인할 수 없음 → 점수 근거에서 제외됨
        with self._lock:
            if url in self.url_to_sid:
                sid = self.url_to_sid[url]
                if source_type == "company":  # 홈페이지로 확인되면 유형 갱신
                    self.sources[sid]["source_type"] = "company"
                return sid
        sid = self.new_id("src")
        # publisher · source_type은 공통 계약 밖의 부가 필드. 평가 노드가 식약처 공식 기록 판별에 쓰므로 유지할 것
        src = S.Source(source_id=sid, title=(title or url)[:300], url=url, publisher=publisher,
                       source_type=source_type, published_at=pub, collected_at=self.checked_at).model_dump()
        with self._lock:
            self.sources[sid] = src
            self.url_to_sid[url] = sid
        return sid


# ══════════════════════════════════════════════
# 외부 호출 (동시 실행 제한·일시 오류 재시도)
# ══════════════════════════════════════════════
def http_get_json(ctx: CollectRun, url: str, params: dict, allow_404: bool = False) -> tuple[Optional[dict], Optional[str]]:
    cached = _cache_get("http", url, params)
    if cached is not None:
        return cached, None
    err = None
    for _ in range(1 + TRANSIENT_RETRY):
        try:
            with _CALL_SEM:
                r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if allow_404 and r.status_code == 404:
                _cache_put("http", url, params, {})
                return {}, None
            if r.status_code >= 500:
                raise _Transient(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            _cache_put("http", url, params, data)
            return data, None
        except (requests.Timeout, requests.ConnectionError, _Transient) as e:
            err = f"{type(e).__name__}: {e}"[:300]
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"[:300]
    return None, err


def _hit(tool: str, query: str, ok: bool, error: Optional[str], **payload) -> dict:
    return {"tool": tool, "query": query, "ok": ok, "error": error, **payload}


def _item_values(items: list[dict], field: str) -> list[str]:
    """응답 item들에서 field(대소문자 무시) 값을 모은다. 예: 품목허가 결과의 PRDLST_NM"""
    if not field:
        return []
    f = field.lower()
    vals = [str(v).strip() for it in items for k, v in it.items() if k.lower() == f and v]
    return _dedupe(vals)


def _extract_items(data: dict) -> list[dict]:
    body = data.get("body") or data.get("response", {}).get("body", {}) or {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item", items)
    if isinstance(items, dict):
        items = [items]
    return items if isinstance(items, list) else []


def mfds_query(ctx: CollectRun, api: dict, param: str, value: str, match: Optional[list[str]] = None) -> dict:
    """match가 주어지면 업체명이 정확히 같은 품목만 남긴다 (식약처 검색은 부분 일치라 다른 회사가 섞임)"""
    key = (api["name"], param, value, tuple(match or []))
    if key in ctx.memo:
        return ctx.memo[key]
    if not (MFDS_API_KEY and api["operation"] and param and value):
        hit = _hit(api["name"], value or "", False, "API 설정 없음 (.env 확인)", items=[])
        ctx.memo[key] = hit
        return hit
    endpoint = f'{api["base"]}/{api["operation"]}'
    data, err = http_get_json(ctx, endpoint, {"serviceKey": MFDS_API_KEY, "pageNo": 1, "numOfRows": 20,
                                              "type": "json", param: value})
    if err:
        hit = _hit(api["name"], value, False, err, items=[])
    else:
        items = _extract_items(data or {})
        if match:
            kept = []
            for it in items:
                names = _item_companies(it)
                if not names or any(same_company(n, m) for n in names for m in match if m):
                    kept.append(it)
            items = kept
        sid = None
        if items:
            public_url = f"{endpoint}?{urllib.parse.urlencode({param: value})}"  # 키 제외
            sid = ctx.add_source(f"{api['name']} 조회: {value}", public_url, "식품의약품안전처", "official")
        hit = _hit(api["name"], value, True, None, source_id=sid, items=items)
    ctx.memo[key] = hit
    return hit


@lru_cache(maxsize=1)
def _tavily():
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None
    from tavily import TavilyClient
    return TavilyClient(api_key=key)


def web_search(ctx: CollectRun, query: str, max_results: int = 5) -> dict:
    key = ("web", query)
    if key in ctx.memo:
        return ctx.memo[key]
    client = _tavily()
    if client is None:
        return _hit("웹 검색", query, False, "TAVILY_API_KEY 없음", results=[])
    err = None
    for attempt in range(1 + WEB_RETRY):
        cached = _cache_get("web", "tavily", {"q": query, "n": max_results})
        try:
            res = cached
            if res is None:
                with _CALL_SEM:
                    res = client.search(query=query, max_results=max_results)
                _cache_put("web", "tavily", {"q": query, "n": max_results}, res)
            results = []
            for x in res.get("results", []):
                url = x.get("url") or ""
                pub_date = x.get("published_date") or date_from_url(url)
                sid = ctx.add_source(x.get("title") or url, url, _publisher_of(url),
                                     source_type_of(url, ctx.company.homepage), pub_date)
                if sid:  # 기준일 이후 자료는 제외됨
                    results.append({"source_id": sid, "title": x.get("title"), "url": url,
                                    "published_at": ctx.sources[sid]["published_at"],
                                    "content": (x.get("content") or "")[:800]})
            hit = _hit("웹 검색", query, True, None, results=results)
            ctx.memo[key] = hit
            return hit
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
            time.sleep(min(30, 3 * 2 ** attempt))  # 요청 한도(429) 등 일시 오류는 기다렸다 재시도
    print(f"  [웹 검색 실패] {ctx.company.display_name}: {query} → {err}", file=sys.stderr)
    return _hit("웹 검색", query, False, err, results=[])


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_page(ctx: CollectRun, url: str) -> dict:
    """페이지 본문 수집: Tavily extract → 실패 시 직접 요청"""
    key = ("page", url)
    if key in ctx.memo:
        return ctx.memo[key]
    text, err = None, None
    cached = _cache_get("page", url, {})
    if cached is not None:
        text = cached
    else:
        client = _tavily()
        if client is not None:
            try:
                with _CALL_SEM:
                    res = client.extract(urls=[url])
                rows = res.get("results", []) if isinstance(res, dict) else []
                text = (rows[0].get("raw_content") if rows else None) or None
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
        if not text:
            try:
                with _CALL_SEM:
                    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                r.encoding = r.apparent_encoding or r.encoding
                text = _html_to_text(r.text)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
        if text:
            _cache_put("page", url, {}, text)
    if not text:
        hit = _hit("페이지", url, False, err or "본문 없음", text="")
    else:
        sid = ctx.add_source(url, url, _publisher_of(url), source_type_of(url, ctx.company.homepage),
                             date_from_url(url))
        hit = _hit("페이지", url, True, None, source_id=sid, text=text[:20000])
    ctx.memo[key] = hit
    return hit


def _pubmed_abstracts(ctx: CollectRun, ids: list[str], extra: dict) -> dict[str, str]:
    """efetch로 초록 원문 및 저자 소속을 가져온다 (연구 설계·표본 수·다기관·회사 소속 여부 판단용)"""
    if not ids:
        return {}
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", **extra}
    cached = _cache_get("pubmed_xml", url, params)
    xml_text = cached
    if xml_text is None:
        try:
            with _CALL_SEM:
                r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            xml_text = r.text
            _cache_put("pubmed_xml", url, params, xml_text)
        except Exception:
            return {}
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
        for art in root.iter("PubmedArticle"):
            pmid = art.findtext(".//MedlineCitation/PMID") or ""
            parts = []
            for t in art.iter("AbstractText"):
                label = t.get("Label")
                text = "".join(t.itertext()).strip()
                if text:
                    parts.append(f"{label}: {text}" if label else text)
            pub_types = [pt.text for pt in art.iter("PublicationType") if pt.text]
            affils = [a.text.strip() for a in art.iter("Affiliation") if a.text and a.text.strip()]
            affil_str = (" [Affiliations: " + "; ".join(affils[:10]) + "]") if affils else ""
            if parts or pub_types or affils:
                out[pmid] = ("[" + ", ".join(pub_types) + "] " if pub_types else "") + " ".join(parts) + affil_str
    except ET.ParseError:
        return {}
    return out


def pubmed_search(ctx: CollectRun, term: str, retmax: int = 5) -> dict:
    key = ("pubmed", term)
    if key in ctx.memo:
        return ctx.memo[key]
    eutils = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    extra = {"api_key": os.getenv("NCBI_API_KEY")} if os.getenv("NCBI_API_KEY") else {}
    data, err = http_get_json(ctx, f"{eutils}/esearch.fcgi",
                              {"db": "pubmed", "term": term, "retmode": "json", "retmax": retmax, **extra})
    if err:
        return _hit("PubMed", term, False, err, papers=[])
    ids = (data or {}).get("esearchresult", {}).get("idlist", [])
    papers = []
    if ids:
        summ, err = http_get_json(ctx, f"{eutils}/esummary.fcgi",
                                  {"db": "pubmed", "id": ",".join(ids), "retmode": "json", **extra})
        if err:
            return _hit("PubMed", term, False, err, papers=[])
        result = (summ or {}).get("result", {})
        abstracts = _pubmed_abstracts(ctx, ids, extra)
        for i in ids:
            if i not in result:
                continue
            doc = result[i]
            url = f"https://pubmed.ncbi.nlm.nih.gov/{i}/"
            sid = ctx.add_source(doc.get("title") or url, url, doc.get("source") or "PubMed", "research",
                                 doc.get("sortpubdate", "")[:10].replace("/", "-") or doc.get("pubdate"))
            if sid:
                papers.append({"source_id": sid, "pmid": i, "title": doc.get("title"),
                               "journal": doc.get("source"), "pubdate": doc.get("pubdate"),
                               "authors": [a.get("name") for a in doc.get("authors", [])][:6],
                               "abstract": abstracts.get(i, "")[:3000]})   # 초록 원문 (연구 유형 포함)
    hit = _hit("PubMed", term, True, None, papers=papers)
    ctx.memo[key] = hit
    return hit


def openfda_510k(ctx: CollectRun, applicant: str) -> dict:
    key = ("openfda", applicant)
    if key in ctx.memo:
        return ctx.memo[key]
    data, err = http_get_json(ctx, "https://api.fda.gov/device/510k.json",
                              {"search": f'applicant:"{applicant}"', "limit": 10}, allow_404=True)
    if err:
        return _hit("openFDA 510(k)", applicant, False, err, results=[])
    results = []
    for x in (data or {}).get("results", []):
        k = x.get("k_number", "")
        url = f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={k}"
        sid = ctx.add_source(f"FDA 510(k) {k} {x.get('device_name', '')}", url, "U.S. FDA", "official",
                             x.get("decision_date"))
        if sid:
            results.append({"source_id": sid, "k_number": k, "device_name": x.get("device_name"),
                            "applicant": x.get("applicant"), "decision_date": x.get("decision_date"),
                            "decision": x.get("decision_description")})
    hit = _hit("openFDA 510(k)", applicant, True, None, results=results)
    ctx.memo[key] = hit
    return hit
