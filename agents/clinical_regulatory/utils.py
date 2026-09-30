from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any, Optional

try:
    from .config import CACHE_DIR, CACHE_ON, SECTION_CHAR_LIMIT
except ImportError:
    from config import CACHE_DIR, CACHE_ON, SECTION_CHAR_LIMIT


# ══════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════
def today() -> str:
    return date.today().isoformat()


def _clip(obj: Any, limit: int = SECTION_CHAR_LIMIT) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " ...(생략)"


def _dedupe(seq) -> list:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _core_name(name: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\s+", " ", name).strip()


_NULLS = {"", "null", "none", "n/a", "na", "-", "없음", "미상"}


def _clean(v: Any) -> Any:
    """LLM이 문자열 "null" 등으로 돌려준 값을 None으로"""
    if isinstance(v, str) and v.strip().lower() in _NULLS:
        return None
    return v


_SUFFIX_RE = re.compile(r"\(주\)|㈜|주식회사|유한회사|\(유\)|유한책임회사|co\.?,?\s*ltd\.?|inc\.?|corp\.?|corporation|\s+", re.I)


def _company_core(name: Optional[str]) -> str:
    return _SUFFIX_RE.sub("", name or "").lower()


def same_company(a: Optional[str], b: Optional[str]) -> bool:
    """법인 형태 표기를 뗀 뒤 이름이 같은지 (부분 일치는 다른 회사로 봄: 케어링 ≠ 케어링아이웨어코리아)"""
    ca, cb = _company_core(a), _company_core(b)
    return bool(ca) and ca == cb


_ENTITY_KEY = re.compile(r"entp|entrps|company|업체|mnft|manuf|bsn.*nm|cmpny", re.I)


def _item_companies(item: dict) -> list[str]:
    return [str(v) for k, v in item.items() if _ENTITY_KEY.search(k) and isinstance(v, str) and v.strip()]


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def norm_date(value: Any) -> Optional[str]:
    """다양한 날짜 표기를 YYYY-MM-DD / YYYY-MM / YYYY 로 정규화"""
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})\b", s)
    if m:
        y, mo, d = map(int, m.groups())
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{4})\s+([A-Za-z]{3})[a-z]*\.?(?:\s+(\d{1,2}))?", s)
    if m and m.group(2).lower() in _MONTHS:
        y, mo = int(m.group(1)), _MONTHS[m.group(2).lower()]
        return f"{y:04d}-{mo:02d}-{int(m.group(3)):02d}" if m.group(3) else f"{y:04d}-{mo:02d}"
    m = re.match(r"^(\d{4})[-./](\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})$", s)
    return m.group(1) if m else None


def strip_fake_jan1(d: Optional[str], raw_text: str = "") -> Optional[str]:
    """연도만 있는 자료에 LLM이 임의로 -01-01을 채워넣은 경우 YYYY로 되돌린다"""
    if not d or not isinstance(d, str):
        return d
    if re.match(r"^\d{4}-01-01$", d):
        if not re.search(r"1\s*월\s*1\s*일|jan(?:uary)?\s*1\b|01[-./]01", raw_text, re.IGNORECASE):
            return d[:4]
    return d


def compare_to_as_of(d: Optional[str], as_of: str) -> str:
    """before / after / ambiguous(같은 연·월이라 판단 불가) / unknown"""
    if not d:
        return "unknown"
    ref = as_of[: len(d)]
    if d < ref:
        return "before"
    if d > ref:
        return "after"
    return "before" if len(d) == 10 else "ambiguous"


_OFFICIAL = ("mfds.go.kr", "fda.gov", "hira.or.kr", "neca.re.kr", "nhis.or.kr", "mohw.go.kr",
             "europa.eu", "pmda.go.jp", ".go.kr", ".gov")
_RESEARCH = ("pubmed", "ncbi.nlm.nih.gov", "doi.org", "clinicaltrials.gov", "nature.com", "sciencedirect",
             "springer", "wiley", "jamanetwork", "thelancet", "nejm", "bmj.com", "rsna.org", "mdpi",
             "frontiersin", "plos", "medrxiv", "arxiv")
_NEWS = ("news", "press", "daily", "times", "herald", "biz", "chosun", "joongang", "donga", "hankyung",
         "mk.co.kr", "etnews", "docdocdoc", "medigatenews", "dailymedi", "yna.co.kr", "newsis", "edaily", "zdnet")


def source_type_of(url: str, homepage: Optional[str]) -> str:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if homepage and host and host in urllib.parse.urlparse(homepage).netloc.lower():
        return "company"
    if any(h in host for h in _OFFICIAL):
        return "official"
    if any(h in host or h in url for h in _RESEARCH):
        return "research"
    if any(h in host for h in _NEWS):
        return "news"
    return "other"


def _publisher_of(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc or "unknown"


# ══════════════════════════════════════════════
# 파일 캐시 및 예외
# ══════════════════════════════════════════════
class _Transient(Exception):
    pass


def _cache_path(kind: str, url: str, params: dict) -> Path:
    safe = {k: v for k, v in params.items() if k not in ("serviceKey", "api_key")}  # 키는 캐시 키에서 제외
    raw = json.dumps([kind, url, safe], sort_keys=True, ensure_ascii=False, default=str)
    return CACHE_DIR / kind / f"{hashlib.sha1(raw.encode('utf-8')).hexdigest()}.json"


def _cache_get(kind: str, url: str, params: dict) -> Optional[Any]:
    if not CACHE_ON:
        return None
    path = _cache_path(kind, url, params)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_put(kind: str, url: str, params: dict, data: Any) -> None:
    if not CACHE_ON:
        return
    path = _cache_path(kind, url, params)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ══════════════════════════════════════════════
# 텍스트 및 제품명 매칭 헬퍼
# ══════════════════════════════════════════════
def _norm_txt(s: str) -> str:
    return re.sub(r"[\s\"'“”‘’]+", "", s or "")


_SLUG_SKIP = {"collections", "browse", "discussions", "rnd", "grants", "investments", "ir", "funding", "startups"}


def source_slug(raw: dict) -> Optional[str]:
    """CSV 출처 URL의 영문 식별자 (예: thevc.kr/raywatt → raywatt). 검색 단서로만 사용하고 사실로 기록하지 않는다."""
    url = (raw.get("출처 URL") or "").strip()
    u = urllib.parse.urlparse(url)
    if "thevc.kr" not in u.netloc:
        return None
    parts = [x for x in u.path.split("/") if x]
    if len(parts) != 1 or parts[0].lower() in _SLUG_SKIP:
        return None
    slug = parts[0]
    return slug if re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{2,}", slug) else None


_DB_DOMAINS = ("thevc.kr", "demoday.co.kr", "groupby.kr", "innoforest.co.kr", "unicornfactory.co.kr",
               "startuprecipe.co.kr", "platum.kr", "venturesquare.net")


def homepage_hint(raw: dict) -> Optional[str]:
    """CSV 출처 URL이 투자 DB·언론이 아니면 회사 홈페이지로 본다"""
    url = (raw.get("출처 URL") or "").strip()
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host or any(d in host for d in _DB_DOMAINS):
        return None
    return f"{urllib.parse.urlparse(url).scheme}://{host}"


def _pkey(name: Optional[str]) -> str:
    """제품명 비교용 키: 공백·하이픈·기호 제거, 소문자 (LuCAS-ABS == LuCAS ABS)"""
    return re.sub(r"[\s\-_·.()\[\]'\"‘’“”]+", "", name or "").lower()


def _mentioned_products(text: str, products: list) -> list[int]:
    """주장 문장에 이름이 나오는 제품의 인덱스. 긴 이름이 짧은 이름을 포함하면 긴 쪽만 인정
    (예: 'LuCAS-ABS' 문장은 LuCAS가 아니라 LuCAS ABS 제품)"""
    t = _pkey(text)
    hits = [i for i, p in enumerate(products) if _pkey(p.name) and _pkey(p.name) in t]
    out = []
    for i in hits:
        longer = [j for j in hits if j != i and _pkey(products[i].name) in _pkey(products[j].name)]
        # 더 긴 이름이 문장에 있고, 짧은 이름이 그 긴 이름 안에서만 등장하면 짧은 쪽은 제외
        if longer and t.count(_pkey(products[i].name)) <= sum(t.count(_pkey(products[j].name)) for j in longer):
            continue
        out.append(i)
    return out


def _merge_products(products: list) -> list:
    """표기만 다른 같은 제품(LuCAS ABS / LuCAS-ABS)은 하나로 합친다"""
    merged: dict[str, Any] = {}
    for p in products:
        k = _pkey(p.name)
        if not k:
            continue
        if k not in merged:
            merged[k] = p
        else:  # 비어 있는 필드는 다른 쪽 값으로 채움
            base = merged[k]
            for f, v in p.model_dump().items():
                if getattr(base, f, None) in (None, "") and v not in (None, ""):
                    setattr(base, f, v)
    return list(merged.values())


_GENERIC_TAIL = re.compile(r"(소프트웨어|솔루션|시스템|장치|플랫폼|프로그램|기기)$")
_CLAIM_KEYS = re.compile(r"진단|치료|예측|검출|판독|정확도|민감도|특이도|AUC|식약처|품목허가|\d\s*등급|FDA|CE\s*인증|임상|효능|"
                         r"예방|성능|검증|논문|혁신의료|신의료기술|비급여|급여 등재", re.I)


def _is_generic_product(name: str, company: str) -> bool:
    """브랜드 없이 '의료영상 분석 소프트웨어'처럼 일반 명칭만 있는 제품"""
    n = (name or "").strip()
    if not n or re.search(r"[A-Za-z]", n) or _company_core(company) in _company_core(n):
        return False
    return bool(_GENERIC_TAIL.search(n.replace(" ", "")))


def _is_product_claim(text: str) -> bool:
    """효능·성능·허가 관련 주장만 채택 (질문형 FAQ, 홍보·안내 문구 제외)"""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) < 8 or t.endswith("?") or t.endswith("나요") or t.endswith("까요"):
        return False
    if re.search(r"(회사|기업|스타트업|업체)(입니다|이다|다)\.?$", t):
        return False  # 회사 소개 문장
    return bool(_CLAIM_KEYS.search(t))
