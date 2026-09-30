"""REFERENCE 표기 형식.

- 기관 보고서: 발행기관(YYYY). *보고서명*. URL
- 학술 논문  : 저자(YYYY). 논문제목. *학술지명*, 권(호), 페이지.
- 웹페이지   : 기관명 또는 작성자(YYYY-MM-DD). *제목*. 사이트명, URL

02~05 출처에는 논문 저자·권·호·페이지가 없어 PubMed 공식 API(E-utilities esummary)로 PMID를 조회해 채운다.
조회 결과는 캐시 파일에 저장하고, 조회하지 못한 항목은 비워 둔다(지어내지 않는다).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

import requests

PUBMED_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PMID = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
REPORT_DOMAINS = ("dart.fss.or.kr", "khidi.or.kr")
REPORT_TYPES = {"market_report", "industry_report", "report"}
MAX_AUTHORS = 3


def pmid(url: Optional[str]) -> Optional[str]:
    m = PMID.search(url or "")
    return m.group(1) if m else None


def kind(ref: dict) -> str:
    """paper / report / web"""
    url = ref.get("url") or ""
    if pmid(url) or ref.get("source_type") == "research":
        return "paper"
    if (ref.get("source_type") in REPORT_TYPES or any(d in url for d in REPORT_DOMAINS)
            or "보고서" in (ref.get("title") or "")):
        return "report"
    return "web"


def fetch_pubmed(pmids: list[str], timeout: float = 10) -> dict[str, dict]:
    """PMID → {authors, year, journal, volume, issue, pages, title}. 실패하면 빈 dict."""
    if not pmids:
        return {}
    try:
        res = requests.get(PUBMED_API, params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
                           timeout=timeout)
        res.raise_for_status()
        data = res.json()["result"]
    except Exception:  # noqa: BLE001 - 조회 실패 시 서지 정보 없이 표기
        return {}
    out = {}
    for uid in data.get("uids", []):
        x = data[uid]
        out[uid] = {"authors": [a["name"] for a in x.get("authors", []) if a.get("authtype") == "Author"],
                    "year": (x.get("pubdate") or "")[:4], "journal": x.get("source"), "volume": x.get("volume"),
                    "issue": x.get("issue"), "pages": x.get("pages") or x.get("elocationid"),
                    "title": x.get("title")}
    return out


def pubmed_metadata(refs: list[dict], cache: Optional[Path] = None,
                    fetch: Callable[[list[str]], dict] = fetch_pubmed) -> dict[str, dict]:
    """REFERENCE 중 PubMed 논문의 서지 정보. cache 파일이 있으면 없는 PMID만 조회한다."""
    cached = json.loads(cache.read_text(encoding="utf-8")) if cache and cache.exists() else {}
    wanted = sorted({p for r in refs if (p := pmid(r.get("url")))})
    missing = [p for p in wanted if p not in cached]
    if missing:
        cached.update(fetch(missing))
        if cache:
            cache.write_text(json.dumps(cached, ensure_ascii=False, indent=1), encoding="utf-8")
    return {p: cached[p] for p in wanted if p in cached}


def _site(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") if url else ""


def _authors(names: list[str]) -> str:
    if not names:
        return "저자 미상"
    return ", ".join(names[:MAX_AUTHORS]) + (" et al." if len(names) > MAX_AUTHORS else "")


def _web_title(title: str) -> tuple[str, Optional[str]]:
    """'제목 < 기업 < 기사본문 - 전자신문' → ('제목', '전자신문'). 매체명이 없으면 None."""
    org = None
    if " - " in title:
        base, tail = title.rsplit(" - ", 1)
        if 0 < len(tail) <= 20:
            title, org = base, tail.strip()
    return title.split(" < ")[0].strip(), org


def format_reference(ref: dict, meta: Optional[dict] = None) -> str:
    url = unquote(ref.get("url") or "")
    title = (ref.get("title") or "제목 미상").strip().rstrip(".")
    date = ref.get("published_at") or ""
    k = kind(ref)
    if k == "paper":
        m = meta or {}
        year = m.get("year") or date[:4] or "연도 미상"
        journal = m.get("journal") or ref.get("publisher") or "학술지 미상"
        vol = m.get("volume") or ""
        issue = f"({m['issue']})" if m.get("issue") else ""
        parts = [f"*{journal}*"] + [x for x in (f"{vol}{issue}", m.get("pages")) if x]
        return f"{_authors(m.get('authors') or [])}({year}). {(m.get('title') or title).rstrip('.')}. {', '.join(parts)}."
    if k == "report":
        publisher = ref.get("publisher") or _site(url) or "발행기관 미상"
        audit = re.match(r"^(.+?)\s+((?:연결)?감사보고서.*)$", title)
        if "dart.fss.or.kr" in url and audit:  # DART 감사보고서: 발행기관 = 기업명
            publisher, title = audit.groups()
        return f"{publisher}({date[:4] or '연도 미상'}). *{title}*. {url}"
    title, org = _web_title(title)
    publisher = (ref.get("publisher") or "").removeprefix("www.")
    author = org or publisher or _site(url) or "작성자 미상"
    site = org or publisher or _site(url) or "사이트 미상"
    return f"{author}({date[:10] or '날짜 미상'}). *{title}*. {site}, {url}"
