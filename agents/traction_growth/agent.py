"""실적·성장성 분석 에이전트(4번) — 전체 워크플로 (단일 파일).

기준: 「04_실적·성장성_분석_변현준_v2」(COMMON CONTRACT 2.0) · criteria.md · schema.py
같은 폴더: schema.py(출력·입력 계약), prompt.md(LLM 추출 지침), criteria.md(판단 기준서)

실행 (프로젝트 루트):
    python -m agents.traction_growth.agent 레모넥스                   # CSV 기업 행으로 최초 실행
    python -m agents.traction_growth.agent 레모넥스 --as-of 2026-09-30 --no-llm
    python -m agents.traction_growth.agent --all --as-of 2026-09-30   # CSV 전체 → 캐시 채우기 + summary.csv
    (캐시: .cache/http, .cache/llm · --refresh 새로 받기 · --no-cache 끄기)

메인 그래프 연결:
    from agents.traction_growth.agent import traction_growth_node
    graph.add_node("traction_growth", traction_growth_node)
    # 읽기: company_profile(없으면 current_company), review_requests, clinical_analysis, risk_analysis
    # 쓰기: traction_analysis (AnalysisEnvelope dict)

환경변수(.env): DART_API_KEY, TAVILY_API_KEY, DATA_GO_KR_API_KEY, OPENAI_API_KEY
               (선택) TRACTION_LLM_MODEL, TRACTION_LLM_TEMPERATURE, NAVER_CLIENT_ID/SECRET, NPS_API_BASE, NPS_SNAPSHOT_DIR

서브그래프 (수집은 병렬)
    START ─┬─ collect_dart ─┐
           ├─ collect_news ─┴─ extract ─┐
           └─ collect_nps ──────────────┴─ judge ─ END

파일 구성
    1. 공통 도구 (환경변수·이름 정규화·호출 예산·HTTP)
    2. OpenDART 클라이언트 + 감사보고서 매출 파서
    3. 뉴스 검색 (네이버·Tavily) · 국민연금 (스냅샷 CSV·API)
    4. 판정 규칙 (단계·성장·추이·기대치·레드플래그·C5) — 순수 함수
    5. LLM 근거 추출
    6. LangGraph 노드 · 근거 조립 · 판정 · 실행 진입점
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import operator
import os
import re
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Annotated, Any, Iterable, Optional, TypedDict
from urllib.parse import urlparse

import requests
from langgraph.graph import END, START, StateGraph

from . import schema as S
from .schema import ExtractedItem, ExtractionBatch



# ════════════════════════════════════════════════════════════
# 1. 공통 도구
# ════════════════════════════════════════════════════════════
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache"
_SEM = threading.BoundedSemaphore(S.RUNTIME_DEFAULTS["external_call_concurrency"])


# ─────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────
def load_env() -> None:
    """프로젝트 루트 .env → os.environ (이미 있는 값은 덮어쓰지 않음)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_name(s: str) -> str:
    """법인명 비교용: (주)·주식회사·공백·기호 제거, 소문자."""
    s = re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(재\)|재단법인|\bco\.,?\s*ltd\b\.?|\binc\b\.?|\bcorp\b\.?",
               "", s or "", flags=re.I)
    return re.sub(r"[\s\W_]+", "", s).lower()


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def search_log(query: str, status: str, tool: str, names: list[str],
               error: Optional[str] = None, urls: Optional[list[str]] = None) -> dict:
    """schema.SearchLog 형태(PDF 필드만). 도구 이름은 query 앞에 '[tavily]'처럼 붙인다.
    '_urls'는 judge 단계에서 source_ids로 바꾸는 내부 필드."""
    return {"query": f"[{tool}] {query}", "searched_at": now_iso(), "status": status,
            "entity_names": list(names), "source_ids": [], "error": error, "_urls": list(urls or [])}


def log_tool(entry: dict) -> str:
    m = re.match(r"^\[(\w+)\]", entry.get("query") or "")
    return m.group(1) if m else ""


def log_query(entry: dict) -> str:
    return re.sub(r"^\[\w+\]\s*", "", entry.get("query") or "")


class Budget:
    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.max_calls:
                return False
            self.used += 1
            return True

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)


# ── 디스크 캐시 (.cache/http, .cache/llm) ─────────────────────
# 끄기: TRACTION_CACHE=0 / 무시하고 새로 받기: TRACTION_CACHE_REFRESH=1
# 캐시 키에서 인증키 파라미터는 뺀다(키 재발급해도 캐시 유지, 캐시 파일에 키가 남지 않음).
SECRET_PARAMS = {"crtfc_key", "serviceKey"}
CACHE_TTL_HOURS = [                 # (URL에 포함된 문자열, 유효 시간) — 먼저 맞는 것 적용, -1 = 영구
    ("/document.xml", -1),          # 접수된 감사보고서 원문은 바뀌지 않음 (정정은 새 접수번호)
    ("opendart.fss.or.kr", 24 * 7),
    ("apis.data.go.kr", 24 * 7),
    ("api.tavily.com", 24 * 7),
    ("openapi.naver.com", 24 * 7),
]


def cache_on() -> bool:
    return os.environ.get("TRACTION_CACHE", "1") != "0"


def cache_refresh() -> bool:
    return os.environ.get("TRACTION_CACHE_REFRESH", "0") == "1"


class CachedResponse:
    """requests.Response 대신 쓰는 최소 객체 (content·text·json·status_code)."""
    from_cache = True

    def __init__(self, content: bytes, status_code: int, created_at: str):
        self.content, self.status_code, self.created_at = content, status_code, created_at

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


def _http_cache_key(method: str, url: str, kw: dict) -> str:
    params = {k: v for k, v in (kw.get("params") or {}).items() if k not in SECRET_PARAMS}
    raw = json.dumps([method, url, sorted(params.items()), kw.get("json")], ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _ttl_hours(url: str) -> float:
    for pat, h in CACHE_TTL_HOURS:
        if pat in url:
            return h
    return 24.0


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _is_zip(r) -> bool:
    return r.content[:2] == b"PK"


def _dart_json_ok(r) -> bool:
    try:
        return r.json().get("status") in ("000", "013")
    except ValueError:
        return False


class Http:
    def __init__(self, budget: Budget):
        self.budget = budget
        self.cache_hits = 0

    def request(self, method: str, url: str, validate=None, **kw):
        """(응답, 오류). 캐시 적중은 호출 예산을 쓰지 않는다. 오류 문자열에는 URL·키를 넣지 않는다.

        validate(응답)->bool 을 통과한 응답만 디스크에 저장한다. DART·국민연금은 오류도 HTTP 200으로 오므로
        (키 오류·호출 한도 초과 등) 검증 없이 저장하면 오류가 캐시 기간 내내 재사용된다."""
        key = _http_cache_key(method, url, kw)
        path = CACHE_DIR / "http" / key[:2] / f"{key}.bin"
        meta = path.with_suffix(".json")
        if cache_on() and not cache_refresh() and path.exists() and meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                ttl = _ttl_hours(url)
                if ttl < 0 or time.time() - m["ts"] < ttl * 3600:
                    cached = CachedResponse(path.read_bytes(), m["status"], m["created_at"])
                    if validate is None or validate(cached):
                        self.cache_hits += 1
                        return cached, None
            except (OSError, ValueError, KeyError):
                pass                                         # 깨진 캐시 → 미스로 보고 새로 받음
        r, err = self._fetch(method, url, **kw)
        if r is not None and cache_on() and (validate is None or validate(r)):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(path, r.content)
                _atomic_write(meta, json.dumps({"url": url, "method": method, "status": r.status_code,
                                                "ts": time.time(),
                                                "created_at": datetime.now().isoformat(timespec="seconds")}).encode())
            except OSError:
                pass
        return r, err

    def _fetch(self, method: str, url: str, **kw) -> tuple[Optional[requests.Response], Optional[str]]:
        err = None
        for _ in range(1 + S.RUNTIME_DEFAULTS["transient_retry"]):
            if not self.budget.take():
                return None, "budget_exhausted"
            try:
                with _SEM:
                    r = requests.request(method, url, timeout=S.RUNTIME_DEFAULTS["call_timeout_sec"], **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                err = type(e).__name__
                time.sleep(0.5)
                continue
            except requests.RequestException as e:
                return None, type(e).__name__
            if r.status_code == 429 or r.status_code >= 500:
                err = f"HTTP {r.status_code}"
                time.sleep(0.5)
                continue
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            return r, None
        return None, err


# ════════════════════════════════════════════════════════════
# 2. OpenDART 클라이언트 · 감사보고서 매출 파서
# ════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────
# OpenDART
# ─────────────────────────────────────────────
class DartClient:
    BASE = "https://opendart.fss.or.kr/api"
    _index: Optional[dict] = None
    _index_lock = threading.Lock()

    def __init__(self, key: str, http: Http):
        self.key = key
        self.http = http

    def _corp_index(self) -> tuple[Optional[dict], Optional[str]]:
        """정규화 이름 → [(corp_code, corp_name, stock_code)]. corpCode.xml은 7일 캐시."""
        with DartClient._index_lock:
            if DartClient._index is not None:
                return DartClient._index, None
            path = CACHE_DIR / "CORPCODE.xml"
            err = None
            if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
                r, err = self.http.request("GET", f"{self.BASE}/corpCode.xml", validate=_is_zip,
                                           params={"crtfc_key": self.key})
                if r is not None:
                    try:
                        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                            CACHE_DIR.mkdir(exist_ok=True)
                            _atomic_write(path, z.read(z.namelist()[0]))
                    except zipfile.BadZipFile:
                        err = "DART corpCode 응답이 zip 아님(키 확인)"
            if not path.exists():
                return None, err or "corpCode 없음"
            idx: dict[str, list] = {}
            for e in ET.parse(path).getroot().iter("list"):
                name = e.findtext("corp_name") or ""
                idx.setdefault(norm_name(name), []).append(
                    (e.findtext("corp_code"), name, (e.findtext("stock_code") or "").strip()))
            DartClient._index = idx
            return idx, None

    def find_corp(self, names: list[str]) -> tuple[Optional[list], Optional[str]]:
        """이름이 정확히(정규화 후) 일치하는 법인 목록. 부분 일치는 쓰지 않는다."""
        idx, err = self._corp_index()
        if idx is None:
            return None, err
        for n in names:
            hits = idx.get(norm_name(n))
            if hits:
                return hits, None
        return [], None

    def _json(self, endpoint: str, **params) -> tuple[Optional[dict], Optional[str]]:
        r, err = self.http.request("GET", f"{self.BASE}/{endpoint}", validate=_dart_json_ok,
                                   params={"crtfc_key": self.key, **params})
        if r is None:
            return None, err
        try:
            j = r.json()
        except ValueError:
            return None, "DART 응답 JSON 아님"
        st = j.get("status")
        if st == "000":
            return j, None
        if st == "013":
            return {"list": []}, None
        return None, f"DART status {st}"

    def audit_reports(self, corp_code: str, bgn: date, end: date) -> tuple[Optional[list], Optional[str]]:
        """외부감사관련(F) 공시 중 감사보고서. 기준일(end) 이후 접수분은 API 범위에서 제외."""
        j, err = self._json("list.json", corp_code=corp_code, bgn_de=bgn.strftime("%Y%m%d"),
                            end_de=end.strftime("%Y%m%d"), pblntf_ty="F", page_count=100)
        if j is None:
            return None, err
        return [x for x in j.get("list", []) if "감사보고서" in x.get("report_nm", "")], None

    def document(self, rcept_no: str) -> tuple[Optional[str], Optional[str]]:
        r, err = self.http.request("GET", f"{self.BASE}/document.xml", validate=_is_zip,
                                   params={"crtfc_key": self.key, "rcept_no": rcept_no})
        if r is None:
            return None, err
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                return "\n".join(z.read(n).decode("utf-8", errors="ignore") for n in z.namelist()), None
        except zipfile.BadZipFile:
            try:
                return None, f"DART status {r.json().get('status')}"     # 키 오류·호출 한도 등
            except ValueError:
                return None, "DART document 응답이 zip 아님"


# DART 원본 XML 표: 과목명 <TD>, 금액 <TE>, 단위·헤더 <TU>/<TH> → 행(TR) 단위로 읽는다.
# 반환 {'label','unit','current','prior','raw_cells'} / 못 찾으면 None → LLM 추출로 넘긴다.
REVENUE_LABELS = ("매출액", "영업수익", "수익(매출액)", "매출", "수익")
IS_TITLES = ("포괄손익계산서", "손익계산서")
UNIT_MAP = {"백만원": 1_000_000, "천원": 1_000, "원": 1}


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&cr;", " ")
    return re.sub(r"\s+", " ", s).strip()


def _label_key(s: str) -> str:
    # 'I. 영업수익', 'Ⅰ.매출액', '1. 매출액(주석 20)' → '영업수익', '매출액'
    s = re.sub(r"^[\sⅠⅡⅢⅣⅤIVX0-9\.\-가-하]{0,4}\.\s*", "", s) if re.match(r"^[ⅠⅡⅢⅣⅤIVX0-9]+\.", s) else s
    s = re.sub(r"\(주석[^)]*\)|\(주[^)]*\)", "", s)
    return s.replace(" ", "")


NOTE_RE = re.compile(r"^\d{1,2}(,\s*\d{1,2})*$")   # 주석번호: '26', '4,23', '5,22,33'
MONEY_RE = re.compile(r"^[\(\-△]?\d{1,3}(,\d{3})*\)?$|^[\(\-△]?\d+\)?$")
DASH = {"-", "–", "—", "−"}


def _num(s: str) -> Optional[int]:
    """금액 셀 → int. '-'는 0, '(1,234)'·'△1,234'는 음수. 금액 형식이 아니면 None."""
    t = s.replace(" ", "")
    if t in DASH:
        return 0
    if not MONEY_RE.match(t):
        return None          # '4,23' 같은 주석번호는 3자리 쉼표 규칙에 안 맞아 탈락
    neg = t.startswith(("(", "-", "△"))
    v = int(t.strip("()-△").replace(",", ""))
    return -v if neg else v


UNIT_RE = re.compile(r"단위\s*[:：]?\s*(?:[^<)]*?,\s*)?(백만원|천원|원)")


def _unit_before(text: str, pos: int) -> Optional[int]:
    """매출 행 바로 앞에 나온 마지막 '(단위 : 원)' 표기 = 그 표의 단위."""
    ms = list(UNIT_RE.finditer(text, 0, pos))
    return UNIT_MAP[ms[-1].group(1)] if ms else None


def extract_revenue(xml_text: str) -> Optional[dict]:
    # 손익계산서 제목이 처음 나오는 지점 이후만 본다 (앞쪽 재무상태표·감사의견 제외)
    starts = [xml_text.find(t) for t in IS_TITLES if xml_text.find(t) >= 0]
    if not starts:
        return None
    body = xml_text[min(starts):]

    note_col = None
    for m in re.finditer(r"<TR[^>]*>(.*?)</TR>", body, flags=re.S):
        tr = m.group(1)
        cells = [_clean(c) for _, c in re.findall(r"<(TD|TE|TU|TH)[^>]*>(.*?)</\1>", tr, flags=re.S)]
        if not cells:
            continue
        # 헤더 행에서 '주석' 열 위치 기억
        for i, c in enumerate(cells):
            if c.replace(" ", "") == "주석":
                note_col = i
        key = _label_key(cells[0])
        if key not in REVENUE_LABELS:
            continue
        vals = []
        for i, c in enumerate(cells[1:], start=1):
            if i == note_col or (i == 1 and NOTE_RE.match(c.replace(" ", ""))):
                continue     # 주석번호 열 제외
            n = _num(c)
            if n is not None and c != "":
                vals.append(n)
        if not vals:
            continue  # 과목명만 있는 소제목 행
        unit = _unit_before(body, m.start())
        if unit is None:
            return None  # 단위 불명 → LLM 폴백
        return {
            "label": cells[0],
            "unit": unit,
            "current": vals[0] * unit,
            "prior": vals[1] * unit if len(vals) > 1 else None,
            "raw_cells": cells,
        }
    return None


# ════════════════════════════════════════════════════════════
# 3. 뉴스 검색 · 국민연금
# ════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────
# 뉴스 검색 (본문 크롤링 없이 검색 API가 주는 제목·요약만 사용)
# ─────────────────────────────────────────────
def _json_has(key: str):
    def ok(r) -> bool:
        try:
            return isinstance(r.json().get(key), list)
        except (ValueError, AttributeError):
            return False
    return ok


def _parse_pub(s: Optional[str]) -> Optional[str]:
    """ISO('2024-07-18…') 또는 RFC-2822('Thu, 18 Jul 2024 …') → 'YYYY-MM-DD'. 모르면 None."""
    if not s:
        return None
    m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return None


def _retrieved_at(r) -> str:
    """검색 결과를 실제로 받은 날짜 (캐시 응답이면 처음 받은 날)."""
    return (getattr(r, "created_at", None) or date.today().isoformat())[:10]


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&apos;", "'")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


class NaverNews:
    URL = "https://openapi.naver.com/v1/search/news.json"

    def __init__(self, cid: str, secret: str, http: Http):
        self.headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
        self.http = http

    def search(self, query: str, display: int = 20) -> tuple[Optional[list], Optional[str]]:
        r, err = self.http.request("GET", self.URL, headers=self.headers, validate=_json_has("items"),
                                   params={"query": query, "display": display, "sort": "sim"})
        if r is None:
            return None, err
        out = []
        for it in r.json().get("items", []):
            url = it.get("originallink") or it.get("link")
            try:
                pub = parsedate_to_datetime(it.get("pubDate")).date().isoformat()
            except Exception:
                pub = None
            out.append({"url": url, "title": _strip_html(it.get("title")), "text": _strip_html(it.get("description")),
                        "published_at": pub, "publisher": host_of(url), "tool": "naver",
                        "retrieved_at": _retrieved_at(r)})
        return out, None


class Tavily:
    URL = "https://api.tavily.com/search"

    def __init__(self, key: str, http: Http):
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.http = http

    def search(self, query: str, max_results: int = 5) -> tuple[Optional[list], Optional[str]]:
        r, err = self.http.request("POST", self.URL, headers=self.headers, validate=_json_has("results"),
                                   json={"query": query, "max_results": max_results, "search_depth": "basic"})
        if r is None:
            return None, err
        out = []
        for it in r.json().get("results", []):
            pub = _parse_pub(it.get("published_date"))
            out.append({"url": it.get("url"), "title": _strip_html(it.get("title")), "text": _strip_html(it.get("content")),
                        "published_at": pub, "publisher": host_of(it.get("url", "")), "tool": "tavily",
                        "retrieved_at": _retrieved_at(r)})
        return out, None


# ─────────────────────────────────────────────
# 국민연금 가입 사업장 내역
#   1순위: data/nps/ 에 받아 둔 월별 CSV 스냅샷 (재현성 높음)
#   2순위: 공공데이터포털 OpenAPI (엔드포인트·필드명은 미확인 → NPS_API_BASE 로 교체 가능)
# ─────────────────────────────────────────────
NPS_COLS = {"ym": "자료생성년월", "name": "사업장명", "bizno": "사업자등록번호",
            "count": "가입자수", "state": "사업장가입상태코드"}


class NpsSnapshots:
    def __init__(self, folder: Path):
        self.files = []
        for p in sorted(folder.glob("*.csv")) if folder.exists() else []:
            m = re.search(r"(20\d{2})(\d{2})(\d{2})?", p.name)
            if m:
                self.files.append((date(int(m.group(1)), int(m.group(2)), 1), p))

    def available(self) -> bool:
        return bool(self.files)

    def pick(self, as_of: date, latest_max_age: int, gap: tuple[int, int]) -> list[Path]:
        """최신(기준일 이전 N개월 이내) 1개 + 약 12개월 전 1개."""
        latest = [f for f in self.files if add_months(as_of, -latest_max_age - 1) <= f[0] <= as_of]
        if not latest:
            return []
        L = max(latest)
        base = [f for f in self.files if add_months(L[0], -gap[1]) <= f[0] <= add_months(L[0], -gap[0])]
        return [L[1]] + ([min(base, key=lambda f: abs((f[0] - add_months(L[0], -12)).days))[1]] if base else [])

    @staticmethod
    def lookup(path: Path, names: list[str], bizno6: Optional[str]) -> tuple[list[dict], Optional[str]]:
        targets = {norm_name(n) for n in names}
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp949", "euc-kr"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return [], "인코딩 판별 실패"
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        try:
            ix = {k: header.index(v) for k, v in NPS_COLS.items()}
        except ValueError:
            return [], "필수 컬럼 없음(자료생성년월·사업장명·사업자등록번호·가입자수)"
        rows = []
        for row in reader:
            if len(row) <= max(ix.values()):
                continue
            if norm_name(row[ix["name"]]) not in targets:
                continue
            if bizno6 and not row[ix["bizno"]].startswith(re.sub(r"\D", "", bizno6)[:6]):
                continue
            if row[ix["state"]].strip() not in ("1", ""):     # 1 = 가입 (탈퇴 사업장 제외)
                continue
            rows.append({"ym": row[ix["ym"]], "name": row[ix["name"]], "bizno": row[ix["bizno"]][:6],
                         "count": int(float(row[ix["count"]] or 0))})
        return rows, None


def _nps_ok(r) -> bool:
    return b"<resultCode>00</resultCode>" in r.content or b"<resultCode>0</resultCode>" in r.content


class NpsApi:
    """data.go.kr 국민연금 가입 사업장 내역 API — 활용가이드 v2.0(2025-05-07 개정) 기준.

    - End Point: NpsBplcInfoInqireServiceV2 / 오퍼레이션명 끝에 V2
    - 요청 항목은 카멜케이스 (wkplNm, bzowrRgstNo, seq, dataCrtYm)
    - 사업장 검색 결과는 '월별 누적데이터' → 같은 사업장이 자료생성년월(dataCrtYm)마다 다른 seq로 나온다
    - 제공 범위: 제공 시점 기준 1년치, 3인 이상 법인 사업장만 (가이드 명시)
    - 가이드 문구: "오픈API로 제공되는 데이터는 통계자료로 활용할 수 없습니다" → 개별 기업 관측값으로만 사용
    """
    OPS = {"search": "getBassInfoSearchV2", "detail": "getDetailInfoSearchV2",
           "period": "getPdAcctoSttusInfoSearchV2"}

    def __init__(self, key: str, http: Http):
        self.key = key
        self.http = http
        self.base = os.environ.get("NPS_API_BASE", "https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2")

    MAX_PAGES = 10   # 사업장명 검색은 부분 일치 → 지점이 많은 기업은 여러 페이지 (예산 보호용 상한, 4번 정의)

    def _page(self, op: str, **params) -> tuple[Optional[list[dict]], Optional[str], int]:
        r, err = self.http.request("GET", f"{self.base}/{self.OPS[op]}", validate=_nps_ok,
                                   params={"serviceKey": self.key, "dataType": "xml", **params})
        if r is None:
            return None, err, 0
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            return None, "NPS 응답 XML 아님", 0
        # data.go.kr 게이트웨이 오류(키·한도·서비스 없음)는 <OpenAPI_ServiceResponse><cmmMsgHeader> 형태
        gw = root.findtext(".//returnReasonCode")
        if gw not in (None, "", "00"):
            return None, f"NPS 게이트웨이 {root.findtext('.//errMsg') or ''} ({gw})", 0
        code = root.findtext(".//resultCode")
        if code not in ("00", "0"):
            return None, f"NPS resultCode {code}", 0
        total = int(root.findtext(".//totalCount") or 0)
        return [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")], None, total

    def _items(self, op: str, **params) -> tuple[Optional[list[dict]], Optional[str]]:
        items, err, _ = self._page(op, **params)
        return items, err

    def search_all(self, **params) -> tuple[Optional[list[dict]], Optional[str], int]:
        """검색 결과 전체 페이지 (최대 MAX_PAGES). 반환: 항목, 오류, 전체 건수."""
        out, total, page = [], 0, 1
        while page <= self.MAX_PAGES:
            items, err, total = self._page("search", pageNo=page, numOfRows=100, **params)
            if items is None:
                return (out or None), err, total
            out += items
            if not items or len(out) >= total:
                break
            page += 1
        return out, None, total

    @staticmethod
    def name_variants(n: str) -> list[str]:
        """국민연금 사업장명은 '주식회사 OO', '(주)OO' 형태가 많아 표기를 바꿔 검색한다."""
        base = re.sub(r"\(주\)|㈜|주식회사", "", n).strip()
        return list(dict.fromkeys([n, f"주식회사 {base}", f"{base} 주식회사", f"(주){base}", f"{base}(주)", f"㈜{base}"]))

    def find(self, names: list[str], bizno6: Optional[str]) -> tuple[Optional[list[dict]], Optional[str], dict]:
        """사업장명이 법인명과 정확히 일치(정규화)하는 사업장의 월별 기록.

        검색은 부분 일치라 '(주)케어링 방문요양센터 경주점' 같은 지점 사업장도 함께 온다.
        지점은 사업자등록번호가 달라 같은 법인인지 확정할 수 없으므로 합산하지 않고 개수만 알린다.
        반환 info: {"total": 전체 건수, "branches": 지점(부분 일치) 사업장 수, "truncated": 페이지 상한 도달}"""
        targets = {norm_name(n) for n in names}
        bizno6 = re.sub(r"\D", "", bizno6 or "")[:6] or None
        queries = [{"bzowrRgstNo": bizno6}] if bizno6 else []
        queries += [{"wkplNm": v} for name in names for v in self.name_variants(name)]
        info = {"total": 0, "branches": 0, "truncated": False}
        for q in queries:
            items, err, total = self.search_all(**q)
            if items is None:
                return None, err, info
            if not items:
                continue                                   # 표기만 바꿔 다시 검색
            hits = [i for i in items if norm_name(i.get("wkplNm", "")) in targets
                    and (not bizno6 or i.get("bzowrRgstNo", "").startswith(bizno6))
                    and i.get("wkplJnngStcd", "1") == "1"]
            others = {(i.get("wkplNm"), i.get("bzowrRgstNo")) for i in items
                      if norm_name(i.get("wkplNm", "")) not in targets
                      and any(norm_name(i.get("wkplNm", "")).startswith(t) for t in targets)}
            info = {"total": total, "branches": len(others), "truncated": len(items) < total}
            if hits:
                return hits, None, info
        return [], None, info

    def detail(self, seq: str) -> tuple[Optional[dict], Optional[str]]:
        items, err = self._items("detail", seq=seq)
        if items is None:
            return None, err
        return (items[0] if items else None), None

    def monthly_flow(self, seq: str, ym: str) -> tuple[Optional[dict], Optional[str]]:
        items, err = self._items("period", seq=seq, dataCrtYm=ym)
        if items is None:
            return None, err
        return (items[0] if items else None), None


# ════════════════════════════════════════════════════════════
# 4. 판정 규칙 (criteria.md 1장) — 순수 함수, 같은 관측값이면 같은 결과
# ════════════════════════════════════════════════════════════
STAGE_RANK = {"none": 0, "D": 1, "C": 2, "B": 3, "A": 4}
EXCLUDED_CONTRACT = {"cancelled", "terminated"}


# ─────────────────────────────────────────────
# 날짜
# ─────────────────────────────────────────────
def parse_date(s) -> tuple[Optional[date], Optional[str]]:
    """'2026-03-15' / '2026-03' / '2026.03' / '20260315' / '2026' → (date, 정밀도)."""
    if not s:
        return None, None
    m = re.match(r"^\s*(\d{4})(?:[-./]?(\d{1,2}))?(?:[-./]?(\d{1,2}))?", str(s))
    if not m:
        return None, None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    try:
        if d:
            return date(int(y), int(mo), int(d)), "day"
        if mo:
            return date(int(y), int(mo), 1), "month"
        return date(int(y), 1, 1), "year"
    except ValueError:
        return None, None


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _span(d: date, prec: str) -> tuple[date, date]:
    if prec == "day":
        return d, d
    if prec == "month":
        return d, d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return d, date(d.year, 12, 31)


def in_window(s, start: date, end: date) -> str:
    """'in' / 'out' / 'uncertain'(월·연 단위 날짜가 경계에 걸림) / 'unknown'(날짜 없음)."""
    d, prec = parse_date(s)
    if d is None:
        return "unknown"
    lo, hi = _span(d, prec)
    if hi < start or lo > end:
        return "out"
    if lo >= start and hi <= end:
        return "in"
    return "uncertain"


def _ok(ids: Iterable[str], confirmed: set[str]) -> bool:
    return any(i in confirmed for i in ids or [])


def _paid_recognized(c: dict, confirmed: set[str]) -> bool:
    return (c.get("is_paid") is True and c.get("independent_confirmation") is True
            and c.get("contract_status") not in EXCLUDED_CONTRACT and _ok(c.get("evidence_ids"), confirmed))


# ─────────────────────────────────────────────
# 1-3. 상업화 단계
# ─────────────────────────────────────────────
def commercial_stage(as_of: date, revenue: list, contracts: list, activities: list,
                     confirmed: set[str]) -> tuple[str, list[str], list[str]]:
    """(단계, 근거 evidence_ids, 날짜 경계 메모)."""
    start = add_months(as_of, -S.STAGE_WINDOW_MONTHS)
    cands: dict[str, list[str]] = {"A": [], "B": [], "C": [], "D": []}
    notes: list[str] = []

    def put(level, date_str, ids, label):
        w = in_window(date_str, start, as_of)
        if w == "in":
            cands[level].extend(ids)
        elif w == "uncertain":
            notes.append(f"{label}({date_str})가 {S.STAGE_WINDOW_MONTHS}개월 경계에 걸려 포함 여부 미확정")

    for r in revenue:
        if (r.get("value") or 0) > 0 and _ok(r.get("evidence_ids"), confirmed):
            put("A", r.get("period_end"), r["evidence_ids"], f"{r.get('fiscal_year')}년 매출")
    for c in contracts:
        if _paid_recognized(c, confirmed):
            put("B", c.get("event_date"), c["evidence_ids"], f"{c.get('counterparty_name')} 유료 계약")
    for a in activities:
        if not _ok(a.get("evidence_ids"), confirmed):
            continue
        level = {"poc": "C", "mou": "D", "award": "D"}.get(a.get("type"))
        if level:
            put(level, a.get("date"), a["evidence_ids"], f"{a.get('type')}")

    for level in ("A", "B", "C", "D"):
        if cands[level]:
            return level, sorted(set(cands[level])), notes
    return "none", [], notes


# ─────────────────────────────────────────────
# 1-4. 매출 시계열 · CAGR · 성장 구간
# ─────────────────────────────────────────────
def revenue_by_year(revenue: list, confirmed: set[str], as_of: date) -> tuple[dict, Optional[str]]:
    """회사 전체·인정 근거·같은 회계 범위만 모은 연도별 매출.
    범위는 연도가 많은 쪽, 동률이면 연결(자회사로 사업을 옮긴 경우 별도 매출만 보면 역성장으로 오판)."""
    groups: dict[str, dict[int, dict]] = {}
    for r in revenue:
        if r.get("product_id") or r.get("value") is None or not _ok(r.get("evidence_ids"), confirmed):
            continue
        scope = r.get("accounting_scope") or "unknown"
        if scope == "unknown":
            continue                                   # 범위 불명 값은 같은 시계열에 넣지 않음
        end, _ = parse_date(r.get("period_end"))
        if end is None or end > as_of:
            continue
        g = groups.setdefault(scope, {})
        g.setdefault(int(r["fiscal_year"]), {
            "value": r["value"], "currency": r.get("currency", "KRW"),
            "accounting_scope": scope, "evidence_ids": list(r.get("evidence_ids", [])),
        })
    if not groups:
        return {}, None
    scope = max(groups, key=lambda k: (len(groups[k]), k == "consolidated"))
    return dict(sorted(groups[scope].items())), scope


def growth_tier(r: Optional[float]) -> str:
    if r is None:
        return "G0"
    if r >= S.GROWTH_G1_MIN:
        return "G1"
    if r >= S.GROWTH_G2_MIN:
        return "G2"
    if r >= 0:
        return "G3"
    return "G4"


RESTATE_TOL = 0.005   # 같은 연도 값이 보고서 간 0.5% 넘게 다르면 기준이 다른 값(정정·연결범위 변경)으로 본다


def cagr(by_year: dict, report_values: Optional[dict] = None) -> tuple[Optional[float], str, dict]:
    """(CAGR, 성장 구간, growth_detail). PDF 05: 동일 범위의 연속 3개 결산연도, 시작 > 0, 마지막 ≥ 0일 때
    (last/first)^(1/2) - 1. 누락·음수·범위 차이는 null/G0.

    report_values: {보고서 키: {연도: 값}} — 같은 연도 값이 보고서마다 다르면(정정·연결범위 변경)
    같은 기준의 시계열이 아니므로 '범위 차이'로 보고 G0 처리한다."""
    years = sorted(y for y, v in by_year.items() if v.get("value") is not None)
    detail = {"observed_years": years, "periods": 0, "unavailable_reason": None}
    if not years:
        detail["unavailable_reason"] = "연도별 매출 관측 없음"
        return None, "G0", detail
    last_y = years[-1]
    need = [last_y - 2, last_y - 1, last_y]
    missing = [y for y in need if y not in years]
    if missing:
        detail["unavailable_reason"] = f"연속 3개 결산연도 부족 (누락: {', '.join(map(str, missing))})"
        return None, "G0", detail
    first, last = by_year[need[0]]["value"], by_year[last_y]["value"]
    if len({by_year[y].get("currency") for y in need}) > 1:
        detail["unavailable_reason"] = "통화 불일치"
        return None, "G0", detail
    restated = restated_years(need, report_values)
    if restated:
        detail["unavailable_reason"] = (f"범위 차이: {', '.join(map(str, restated))}년 매출이 감사보고서마다 다름"
                                        "(정정·연결범위 변경) — 같은 기준의 3개년 시계열 아님")
        return None, "G0", detail
    if first <= 0:
        detail["unavailable_reason"] = f"시작 연도({need[0]}) 매출 ≤ 0"
        return None, "G0", detail
    if last < 0:
        detail["unavailable_reason"] = f"마지막 연도({last_y}) 매출 음수"
        return None, "G0", detail
    r = round((last / first) ** 0.5 - 1, 4)
    detail["periods"] = 2
    detail["observed_years"] = need
    return r, growth_tier(r), detail


def restated_years(years: list[int], report_values: Optional[dict]) -> list[int]:
    out = []
    for y in years:
        vals = [rv[y] for rv in (report_values or {}).values() if y in rv]
        if len(vals) > 1 and max(vals) - min(vals) > RESTATE_TOL * max(abs(v) for v in vals):
            out.append(y)
    return out


# ─────────────────────────────────────────────
# 1-4. 고용·고객 추이
# ─────────────────────────────────────────────
def trend(observations: list[tuple], as_of: date) -> tuple[str, dict]:
    """observations: (date_str, value, group_key, evidence_ids) 목록.
    최신 관측 as_of 이전 3개월 이내, 비교값 약 12개월 전(±1개월), 같은 group, 기준값 > 0."""
    detail = {"change_rate": None, "period_start": None, "period_end": None,
              "definition": None, "evidence_ids": [], "unavailable_reason": None}
    obs = []
    for ds, v, g, ids in observations:
        d, _ = parse_date(ds)
        if d is not None and v is not None:
            obs.append((d, v, g, ids, ds))
    lo = add_months(as_of, -S.TREND_LATEST_MAX_AGE_MONTHS).replace(day=1)   # 관측이 월초 날짜라 월 단위로 비교
    latest = [o for o in obs if lo <= o[0] <= as_of]
    if not latest:
        detail["unavailable_reason"] = f"최신 관측이 기준일 {S.TREND_LATEST_MAX_AGE_MONTHS}개월 이내에 없음"
        return "unknown", detail
    L = max(latest, key=lambda o: o[0])
    gmin, gmax = S.TREND_BASE_GAP_MONTHS
    b_lo, b_hi, target = add_months(L[0], -gmax), add_months(L[0], -gmin), add_months(L[0], -12)
    bases = [o for o in obs if o[2] == L[2] and b_lo <= o[0] <= b_hi]
    if not bases:
        detail["unavailable_reason"] = "같은 정의의 약 12개월 전(±1개월) 비교값 없음"
        return "unknown", detail
    B = min(bases, key=lambda o: abs((o[0] - target).days))
    detail.update(period_start=B[4], period_end=L[4], definition=L[2],
                  evidence_ids=sorted(set(B[3]) | set(L[3])))
    if B[1] <= 0:
        detail["unavailable_reason"] = "기준값 ≤ 0"
        return "unknown", detail
    rate = round((L[1] - B[1]) / B[1], 4)
    detail["change_rate"] = rate
    if rate >= S.TREND_UP_MIN:
        return "up", detail
    if rate <= -S.TREND_UP_MIN:
        return "down", detail
    return "flat", detail


# ─────────────────────────────────────────────
# 1-5. 투자 단계 기대치
# ─────────────────────────────────────────────
def normalize_invest_stage(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = re.sub(r"[\s_]", "", str(raw)).lower().replace("시리즈", "series").replace("프리", "pre-")
    t = re.sub(r"pre-?series-?", "pre-", t)          # 'Pre-Series A'·'프리시리즈A' → 'pre-a'
    if "ipo" in t:
        return "Pre-IPO"
    if re.search(r"pre-?seed", t):
        return "Pre-seed"
    if "seed" in t or "시드" in t or "엔젤" in t:
        return "Seed"
    m = re.search(r"pre-?([a-e])\b|pre-?([a-e])$", t)
    if m:
        letter = (m.group(1) or m.group(2)).upper()
        key = f"Pre-{letter}"
        return key if key in S.STAGE_EXPECTATION else None
    m = re.search(r"series-?([a-e])", t) or re.fullmatch(r"([a-e])\+?", t)
    if m:
        key = f"Series {m.group(1).upper()}"
        return key if key in S.STAGE_EXPECTATION else None
    return None


def expectation(invest_stage: Optional[str], stage: str, none_reason: Optional[str]) -> tuple[str, Optional[str]]:
    key = normalize_invest_stage(invest_stage)
    if key is None:
        return "unknown", None
    req = S.STAGE_EXPECTATION[key]
    if stage == "none":
        # 4번 정의: 조회를 끝냈는데 인정 근거가 없으면 미달, 조사 미완료면 판단 불가
        return ("not_met" if none_reason == "no_recognized_evidence" else "unknown"), req
    return ("met" if STAGE_RANK[stage] >= STAGE_RANK[req] else "not_met"), req


# ─────────────────────────────────────────────
# 1-6. 레드플래그
# ─────────────────────────────────────────────
def red_flags(as_of: date, contracts: list, activities: list, confirmed: set[str],
              tier: str, revenue_cagr: Optional[float], growth_ids: list[str],
              headcount_detail: dict, expectation_status: str, invest_stage: Optional[str],
              stage: str, required: Optional[str], contract_search_ok: bool) -> tuple[list[dict], list[str]]:
    """(flags[{code, reason, evidence_ids, status}], 날짜 경계 메모). 조건은 PDF 05 레드플래그 표 그대로."""
    flags, notes = [], []
    start24 = add_months(as_of, -S.RF_WINDOW_MONTHS)
    period = f"{start24.isoformat()}~{as_of.isoformat()}"

    paid = [c for c in contracts if _paid_recognized(c, confirmed)]
    paid_in = [c for c in paid if in_window(c.get("event_date"), start24, as_of) == "in"]
    paid_unc = [c for c in paid if in_window(c.get("event_date"), start24, as_of) == "uncertain"]
    for c in paid_unc:
        notes.append(f"{c.get('counterparty_name')} 계약({c.get('event_date')})이 24개월 경계에 걸림 → RF1 확정 보류")

    # RF1
    if contract_search_ok and not paid_in and not paid_unc:
        flags.append({"code": "RF1", "status": "needs_review", "evidence_ids": [],
                      "reason": f"기준일 직전 24개월({period})에 인정 신규 유료 계약을 찾지 못함 (계약 없음 확정 아님)"})

    # RF2
    rf2_reasons, rf2_ids = [], []
    if tier == "G4":
        rf2_reasons.append(f"비교 가능한 매출 감소 (CAGR {revenue_cagr:.1%}, G4)")
        rf2_ids += growth_ids
    hc = headcount_detail.get("change_rate")
    if hc is not None and hc <= S.RF2_HEADCOUNT_DROP:
        rf2_reasons.append(f"12개월 고용 감소율 {hc:.1%} ({headcount_detail.get('period_start')}→{headcount_detail.get('period_end')})")
        rf2_ids += headcount_detail.get("evidence_ids", [])
    if rf2_reasons:
        flags.append({"code": "RF2", "status": "confirmed", "evidence_ids": sorted(set(rf2_ids)),
                      "reason": " / ".join(rf2_reasons) + " — 수치 사실, 원인은 실사 대상"})

    # RF3
    def in24(x, key):
        return in_window(x.get(key), start24, as_of) == "in"
    mous = [a for a in activities if a.get("type") == "mou" and _ok(a.get("evidence_ids"), confirmed) and in24(a, "date")]
    # 상대 기관이 없는 MOU 기사는 같은 달이면 같은 MOU로 본다 (기사 여러 건 = 협약 여러 건 아님)
    distinct = {a.get("counterparty_id") or ("month", (a.get("date") or "")[:7]) for a in mous}
    pocs = [a for a in activities if a.get("type") == "poc" and _ok(a.get("evidence_ids"), confirmed) and in24(a, "date")]
    if len(distinct) >= S.RF3_MIN_MOU and not paid_in and not pocs:
        flags.append({"code": "RF3", "status": "needs_review",
                      "evidence_ids": sorted({i for a in mous for i in a.get("evidence_ids", [])}),
                      "reason": f"24개월 내 서로 다른 MOU {len(distinct)}건, 인정 유료 계약·실증 미발견"})

    # RF4
    if expectation_status == "not_met":
        flags.append({"code": "RF4", "status": "needs_review", "evidence_ids": [],
                      "reason": f"투자 단계 {invest_stage}의 최소 기대 {required} 대비 상업화 단계 {stage}"})
    return flags, notes


# ─────────────────────────────────────────────
# 1-7. C5 점수 입력
# ─────────────────────────────────────────────
def c5(stage: str, tier: str, evidence_ids: list[str]) -> dict:
    s = S.C5_COMMERCIAL_SCORE.get(stage)
    t = S.C5_GROWTH_SCORE.get(tier)
    w_s, w_t = S.C5_WEIGHTS
    out = {"score": None, "score_status": "unknown", "commercial_score": s, "growth_score": t,
           "growth_metric": "revenue_cagr" if t is not None else None,
           "growth_status": "scored" if t is not None else "unknown",
           "evidence_ids": sorted(set(evidence_ids)), "rationale": ""}
    if s is None:
        out["rationale"] = "상업화 인정 근거 없음(none) → 점수 판단불가, 6번이 처리"
        return out
    if t is None:
        out.update(score=float(s), score_status="scored",
                   rationale=f"상업화 {stage}(S={s}). 성장 자료 미확인(G0)으로 S만 사용")
        return out
    out.update(score=round(w_s * s + w_t * t, 2), score_status="scored",
               rationale=f"상업화 {stage}(S={s}) · 성장 {tier}(T={t}) → {w_s}·S + {w_t}·T")
    return out


# ════════════════════════════════════════════════════════════
# 5. LLM 근거 추출 — 등급·점수는 정하지 않는다
# ════════════════════════════════════════════════════════════
PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")
DOC_CHARS = 4000


# OPENAI_API_KEY만 있고 모델명을 안 정했을 때 쓰는 기본값.
# OpenAI 모델 문서(2026-09-30 확인)의 가장 저렴한 권장 모델. 구조화 출력 지원은 실행으로 확인 필요.
DEFAULT_OPENAI_MODEL = "gpt-6-luna"


def get_llm() -> Optional[Any]:
    model = os.environ.get("TRACTION_LLM_MODEL")
    if not model and os.environ.get("OPENAI_API_KEY"):
        model = f"openai:{DEFAULT_OPENAI_MODEL}"
    if not model:
        return None
    from langchain.chat_models import init_chat_model
    # 일부 모델은 temperature 변경을 허용하지 않음(기본값 1만 지원) → 환경변수로 지정할 때만 전달
    t = os.environ.get("TRACTION_LLM_TEMPERATURE")
    return init_chat_model(model, **({"temperature": float(t)} if t else {}))


def _squash(s: Optional[str]) -> str:
    return re.sub(r"\s+", "", s or "")


def excerpt_in_doc(item: ExtractedItem, doc: dict) -> bool:
    """LLM이 준 발췌가 실제 원문에 있는지 (환각 방지)."""
    ex = _squash(item.excerpt)
    return bool(ex) and ex in _squash(doc.get("title", "") + doc.get("text", ""))


def _body(batch: list[dict], names: list[str]) -> str:
    parts = [f"대상 기업: {', '.join(names)}"]
    for d in batch:
        parts.append(
            f"[문서 {d['doc_id']}]\nURL: {d['url']}\n제목: {d.get('title', '')}\n"
            f"게재일: {d.get('published_at') or '미상'}\n출처 유형: {d.get('source_type')}\n"
            f"본문:\n{(d.get('text') or '')[:DOC_CHARS]}")
    return "\n\n".join(parts)


def extract_items(docs: list[dict], names: list[str], llm: Any,
                  batch_size: int = 6, workers: int = 4) -> tuple[list[tuple[ExtractedItem, dict]], list[str]]:
    """반환: [(item, 원문 doc)], 오류 목록. URL이 입력 문서에 없는 item(환각)은 버린다."""
    structured = llm.with_structured_output(ExtractionBatch)
    batches = [docs[i:i + batch_size] for i in range(0, len(docs), batch_size)]

    model_id = f"{type(llm).__name__}:{getattr(llm, 'model_name', None) or getattr(llm, 'model', '')}"

    def run(batch):
        body = _body(batch, names)
        # 캐시 키 = 모델 + 프롬프트 + 입력 문서 → 같은 입력이면 같은 추출 결과 (temperature 1이어도 재현)
        key = hashlib.sha256(f"{model_id}\n{PROMPT}\n{body}".encode()).hexdigest()
        path = CACHE_DIR / "llm" / f"{key}.json"
        if cache_on() and not cache_refresh() and path.exists():
            try:
                return batch, ExtractionBatch.model_validate_json(path.read_text(encoding="utf-8")), None
            except ValueError:
                pass
        try:
            res = structured.invoke([("system", PROMPT), ("human", body)])
        except Exception as e:  # 모델·네트워크 오류는 배치 단위로 기록하고 계속
            return batch, None, f"{type(e).__name__}: {str(e)[:200]}"
        if cache_on() and res is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(res.model_dump_json(), encoding="utf-8")
        return batch, res, None

    out, errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for batch, res, err in ex.map(run, batches):
            if err:
                errors.append(err)
                continue
            by_url = {d["url"]: d for d in batch}
            for it in (res.items if res else []):
                if it.kind == "none":
                    continue
                doc = by_url.get(it.source_url)
                if doc is None:
                    continue
                out.append((it, doc))
    return out, errors


# ════════════════════════════════════════════════════════════
# 6. LangGraph 노드 · 근거 조립 · 판정 · 실행 진입점
# ════════════════════════════════════════════════════════════

RECORD_KEYS = ("revenue_records", "contract_records", "activity_records", "headcount_records", "customer_records")
UNIT_WORDS = {"조원": 10**12, "억원": 10**8, "억": 10**8, "천만원": 10**7, "백만원": 10**6,
              "만원": 10**4, "천원": 10**3, "원": 1, "krw": 1}
NEWS_QUERY_SUFFIX = ["계약", "공급 계약", "도입 병원", "매출", "MOU 협약", "실증 PoC"]
REVIEW_SYNONYMS = ["공급", "납품", "구독", "판매 계약", "수가", "유료 도입"]
NEWS_CATS = {"stage", "rf1", "rf3", "customer", "expectation", "rf4", "c5"}
DART_CATS = {"disclosure", "growth", "rf2", "c5"}
POPULATION_NPS = "국민연금 사업장 가입자 수"


# ─────────────────────────────────────────────
# 입력 프로필 (company_profile 필드명은 1번과 합의 전 → 여러 이름을 받아 준다)
# ─────────────────────────────────────────────
def company_names(profile: dict) -> list[str]:
    raw = [profile.get(k) for k in ("legal_name", "name", "company_name", "기업명")]
    raw += list(profile.get("aliases") or []) + list(profile.get("former_names") or [])
    out, seen = [], set()
    for n in raw:
        if n and norm_name(n) not in seen:
            seen.add(norm_name(n))
            out.append(n)
    return out


def company_id_of(profile: dict) -> str:
    if profile.get("company_id"):
        return str(profile["company_id"])
    names = company_names(profile) or ["unknown"]
    return "c" + hashlib.md5(norm_name(names[0]).encode()).hexdigest()[:6]


def profile_from_csv(name: str, csv_path: Path = ROOT / "data" / "startup_list.csv") -> Optional[dict]:
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("기업명") == name:
                # 행 번호가 아닌 이름 기반 ID (CSV 행이 바뀌어도 유지). CSV에 company_id 열이 있으면 우선
                cid = row.get("company_id") or company_id_of({"legal_name": name})
                prod = (row.get("주요 제품/서비스") or "").strip()
                return {"company_id": cid, "legal_name": name, "aliases": [],
                        "invest_stage": row.get("최근 투자단계"), "invest_stage_date": row.get("최근 투자일") or None,
                        "products": [{"product_id": f"{cid}:p01", "name": prod}] if prod else []}
    return None


def _clue_strings(obj: Any, out: list[str]) -> None:
    """다른 분석 결과에서 제품명·기관명 후보 문자열을 모은다 (보완 검색 단서)."""
    keys = ("product_name", "product", "name", "institution", "institution_name",
            "counterparty_name", "hospital", "partner")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and 2 <= len(v) <= 40:
                out.append(v)
            else:
                _clue_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _clue_strings(v, out)


# ─────────────────────────────────────────────
# 실행 컨텍스트 · 서브그래프 State
# ─────────────────────────────────────────────
@dataclass
class Ctx:
    profile: dict
    as_of: date
    run_id: str
    company_id: str
    names: list[str]
    invest_stage: Optional[str]
    homepage_host: str = ""
    mode: str = "initial"                     # initial | review
    request: Optional[dict] = None
    previous: Optional[dict] = None
    budgets: dict = field(default_factory=dict)   # 수집 노드별 호출 예산 (dart·news·nps) — 병렬 노드끼리 경쟁 안 함
    dart: Optional[DartClient] = None
    naver: Optional[NaverNews] = None
    tavily: Optional[Tavily] = None
    nps_files: Optional[NpsSnapshots] = None
    nps_api: Optional[NpsApi] = None
    llm: Any = None
    run_dart: bool = True
    run_news: bool = True
    run_nps: bool = True
    dart_reports_per_scope: int = 2
    dart_both_scopes: bool = True             # 별도·연결 모두 파싱 → rules가 범위 선택
    news_queries: list[tuple[str, str]] = field(default_factory=list)
    prev_urls: set = field(default_factory=set)


def _merge(a: Optional[dict], b: Optional[dict]) -> dict:
    return {**(a or {}), **(b or {})}


class TGState(TypedDict, total=False):
    docs: Annotated[list, operator.add]
    facts: Annotated[list, operator.add]
    search_log: Annotated[list, operator.add]
    status: Annotated[dict, _merge]
    notes: Annotated[list, operator.add]
    envelope: dict


def _iso8(s: str) -> Optional[str]:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if s and len(s) >= 8 else None


def _month_end(y: int, m: int) -> date:
    return add_months(date(y, m, 1), 1) - timedelta(days=1)


# ─────────────────────────────────────────────
# 수집 노드 1: OpenDART
# ─────────────────────────────────────────────
def make_collect_dart(ctx: Ctx):
    def fy(rep: dict) -> Optional[tuple[int, int]]:
        m = re.search(r"\((\d{4})\.(\d{2})\)", rep.get("report_nm", ""))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def dart_url(rep: dict) -> str:
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rep['rcept_no']}"

    def node(state: TGState) -> dict:
        if not ctx.run_dart:
            return {}
        names = ctx.names
        if ctx.dart is None:
            return {"status": {"disclosure": "not_reviewed"},
                    "search_log": [search_log("DART 감사보고서", "error", "dart", names, "DART_API_KEY 없음")]}
        logs, facts, docs, notes = [], [], [], []

        code, corp_name = ctx.profile.get("dart_corp_code"), names[0]
        if not code:
            hits, err = ctx.dart.find_corp(names)
            if hits is None:
                logs.append(search_log("DART 고유번호 조회", "error", "dart", names, err))
                return {"status": {"disclosure": "search_failed"}, "search_log": logs}
            if not hits:
                logs.append(search_log("DART 고유번호 조회", "not_found", "dart", names))
                return {"status": {"disclosure": "not_found"}, "search_log": logs,
                        "notes": ["DART 고유번호 없음(공시 이력 없음 또는 법인명 불일치) — 재무 비공개로 확정하지 않음"]}
            if len(hits) > 1:
                notes.append(f"DART 동명 법인 {len(hits)}곳 — {hits[0][0]} 사용, 법인 식별자 확인 필요")
            code, corp_name = hits[0][0], hits[0][1]

        bgn = date(ctx.as_of.year - 5, 1, 1)
        query = f"DART 외부감사 공시 corp_code={code} {bgn}~{ctx.as_of}"
        reps, err = ctx.dart.audit_reports(code, bgn, ctx.as_of)
        if reps is None:
            logs.append(search_log(query, "error", "dart", [corp_name], err))
            return {"status": {"disclosure": "search_failed"}, "search_log": logs, "notes": notes}
        reps = [r for r in reps if fy(r)]
        if not reps:
            logs.append(search_log(query, "not_found", "dart", [corp_name]))
            return {"status": {"disclosure": "not_found"}, "search_log": logs, "notes": notes}

        def is_con(rep: dict) -> bool:          # '[기재정정]연결감사보고서 (2024.12)' 도 연결
            return re.sub(r"^\s*\[[^\]]*\]\s*", "", rep["report_nm"]).startswith("연결")
        sep = [r for r in reps if not is_con(r)]
        con = [r for r in reps if is_con(r)]
        groups = [(sep, "separate"), (con, "consolidated")] if ctx.dart_both_scopes else \
                 [(sep, "separate")] if sep else [(con, "consolidated")]
        picked: list[tuple[dict, str]] = []
        for group, scope in groups:
            seen = set()
            for r in sorted(group, key=lambda x: (fy(x), x.get("rcept_dt", "")), reverse=True):
                if fy(r) in seen:
                    continue                     # 정정 보고서 → 가장 최근 접수분만
                seen.add(fy(r))
                picked.append((r, scope))
                if len(seen) >= ctx.dart_reports_per_scope:
                    break
        logs.append(search_log(query, "found", "dart", [corp_name], urls=[dart_url(r) for r, _ in picked]))

        for rep, scope in picked:
            url = dart_url(rep)
            src = {"url": url, "title": f"{corp_name} {rep['report_nm']}", "publisher": "금융감독원 전자공시(DART)",
                   "source_type": "official", "published_at": _iso8(rep.get("rcept_dt", ""))}
            text, err = ctx.dart.document(rep["rcept_no"])
            if text is None:
                logs.append(search_log(f"DART 원문 {rep['rcept_no']}", "error", "dart", [corp_name], err))
                continue
            y, m = fy(rep)
            rev = extract_revenue(text)
            if rev is None:
                i = max(0, text.find("손익계산서"))
                plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text[i:i + 20000])).strip()
                docs.append({**src, "text": plain[:6000], "tool": "dart"})
                notes.append(f"{rep['report_nm']} 매출 규칙 파싱 실패 → LLM 추출로 넘김 (범위 unknown)")
                continue
            for which, val, year in (("당기", rev["current"], y), ("전기", rev["prior"], y - 1)):
                if val is None:
                    continue
                end = _month_end(year, m)
                start = add_months(end, -12) + timedelta(days=1)
                facts.append({
                    "kind": "revenue", "source": src, "status": "confirmed",
                    "record": {"period_start": start.isoformat(), "period_end": end.isoformat(), "fiscal_year": year,
                               "value": float(val), "currency": "KRW", "unit": "KRW",
                               "accounting_scope": scope, "product_id": None},
                    "statement": f"{rep['report_nm']} 손익계산서 {rev['label']}: {year}년 {val:,}원 ({which})",
                    "excerpt": " | ".join(rev["raw_cells"]), "locator": f"손익계산서 > {rev['label']} > {which}",
                    "event_date": end.isoformat(), "date_uncertainty": None})
        return {"status": {"disclosure": "found"}, "search_log": logs, "facts": facts, "docs": docs, "notes": notes}
    return node


# ─────────────────────────────────────────────
# 수집 노드 2: 뉴스 (네이버 · Tavily) — 검색 API 요약문만 사용
# ─────────────────────────────────────────────
# 기업정보 집계 사이트: 회사 제출·추정 자료를 재가공 → 독립 확인 아님 (4번 정의)
AGGREGATOR_HOSTS = ("saramin.co.kr", "jobkorea.co.kr", "wanted.co.kr", "jobplanet.co.kr", "catch.co.kr",
                    "rocketpunch.com", "nicebizinfo.com", "kisline.com", "crunchbase.com")
# 약관상 자동 수집·AI 활용 금지 (criteria.md 2-2) → 검색 결과에서 제외
EXCLUDED_HOSTS = ("innoforest.co.kr", "thevc.kr")


def _host_in(h: str, hosts: tuple) -> bool:
    return any(h == x or h.endswith("." + x) for x in hosts)


def _source_type(url: str, homepage_host: str) -> str:
    h = host_of(url)
    if homepage_host and (h == homepage_host or h.endswith("." + homepage_host)):
        return "company"
    if _host_in(h, AGGREGATOR_HOSTS):
        return "other"
    if h.endswith(".go.kr"):
        return "official"
    return "news"


_PARTICLE = r"(?:$|[^가-힣A-Za-z0-9]|은|는|이|가|을|를|의|와|과|에|도|로|측|社)"


def mentions(text: str, name: str) -> bool:
    """본문에 기업명이 나오는지. 정규화 2글자 이하(예: '닷', '아크')는 단어 경계 + 조사까지만 허용
    ('닷컴', '아크릴' 제외). 그보다 긴 이름은 공백·기호를 무시한 부분 일치."""
    n = norm_name(name)
    if not n:
        return False
    if len(n) > 2:
        return n in norm_name(text)
    core = re.sub(r"\(주\)|㈜|주식회사", "", name).strip()
    return re.search(rf"(?<![가-힣A-Za-z0-9]){re.escape(core)}(?={_PARTICLE})", text or "", flags=re.I) is not None


def make_collect_news(ctx: Ctx):
    def node(state: TGState) -> dict:
        if not ctx.run_news:
            return {}
        names = ctx.names
        if not (ctx.naver or ctx.tavily):
            return {"status": {"news": "not_reviewed"},
                    "search_log": [search_log("뉴스 검색", "error", "news", names, "NAVER·TAVILY 키 없음")]}

        def run(task):
            tool, q = task
            client = ctx.naver if tool == "naver" else ctx.tavily
            if client is None:
                return task, None, f"{tool} 키 없음"
            res, err = client.search(q)
            return task, res, err

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(run, ctx.news_queries))

        min_d = add_months(ctx.as_of, -S.NEWS_MAX_AGE_MONTHS)
        logs, docs, seen, n_err, rediscovered = [], [], set(), 0, 0
        for (tool, q), res, err in results:
            if res is None:
                n_err += 1
                logs.append(search_log(q, "error", tool, names, err))
                continue
            kept = []
            for it in res:
                url = it.get("url")
                if not url or url in seen:
                    continue
                if _host_in(host_of(url), EXCLUDED_HOSTS):
                    continue                               # 약관상 사용 제외 사이트
                text = f"{it.get('title') or ''} {it.get('text') or ''}"
                if not any(mentions(text, n) for n in names):
                    continue                               # 대상 기업 언급 없는 결과 제외
                d, _ = parse_date(it.get("published_at"))
                if d and (d > ctx.as_of or d < min_d):
                    continue                               # 기준일 이후·너무 오래된 기사 제외
                seen.add(url)
                kept.append(url)
                if url in ctx.prev_urls:
                    rediscovered += 1                      # 이미 찾은 원문 재발견 → 재추출 안 함
                    continue
                docs.append({**it, "source_type": _source_type(url, ctx.homepage_host)})
            logs.append(search_log(q, "found" if kept else "not_found", tool, names, urls=kept))
        if not results:
            status = "not_reviewed"
        else:
            status = "failed" if n_err == len(results) else ("partial" if n_err else "ok")
        notes = [f"이미 찾은 원문 재발견 {rediscovered}건"] if rediscovered else []
        return {"status": {"news": status}, "search_log": logs, "docs": docs, "notes": notes}
    return node


# ─────────────────────────────────────────────
# 수집 노드 3: 국민연금 (스냅샷 CSV 우선, 없으면 API)
# ─────────────────────────────────────────────
def make_collect_nps(ctx: Ctx):
    def fact(ym: str, count: int, entity: str, url: str, title: str, statement: str) -> dict:
        digits = re.sub(r"\D", "", ym)
        obs = f"{digits[:4]}-{digits[4:6]}-01"
        return {"kind": "headcount", "status": "confirmed", "event_date": obs, "date_uncertainty": None,
                "source": {"url": url, "title": title, "publisher": "국민연금공단", "source_type": "official",
                           "published_at": obs},
                "record": {"observation_date": obs, "count": count, "population": POPULATION_NPS, "entity_id": entity},
                "statement": statement, "excerpt": None, "locator": f"자료생성년월 {digits[:6]}"}

    def node(state: TGState) -> dict:
        if not ctx.run_nps:
            return {}
        names, biz = ctx.names, ctx.profile.get("business_registration_no")
        logs, facts, nps_notes = [], [], []

        if ctx.nps_files and ctx.nps_files.available():
            paths = ctx.nps_files.pick(ctx.as_of, S.TREND_LATEST_MAX_AGE_MONTHS, S.TREND_BASE_GAP_MONTHS)
            if not paths:
                logs.append(search_log("국민연금 스냅샷", "not_found", "nps", names, "기준일 3개월 이내 파일 없음"))
                return {"status": {"nps": "not_found"}, "search_log": logs}
            for p in paths:
                rows, err = NpsSnapshots.lookup(p, names, biz)
                q = f"국민연금 스냅샷 {p.name}"
                if err:
                    logs.append(search_log(q, "error", "nps", names, err))
                    continue
                if len(rows) > 1 and not biz:
                    logs.append(search_log(q, "not_found", "nps", names, f"동명 사업장 {len(rows)}곳 — 사업자등록번호 필요"))
                    continue
                if not rows:
                    logs.append(search_log(q, "not_found", "nps", names))
                    continue
                r = rows[0]
                url = f"https://www.data.go.kr/data/3046071/fileData.do#ym={r['ym']}"
                facts.append(fact(r["ym"], r["count"], f"nps:{r['bizno']}:{norm_name(r['name'])}", url,
                                  f"국민연금 가입 사업장 내역 {r['ym']}",
                                  f"{r['name']} 국민연금 가입자 수 {r['count']}명 ({r['ym']})"))
                logs.append(search_log(q, "found", "nps", names, urls=[url]))
        elif ctx.nps_api:
            # 활용가이드 v2.0: 검색 결과가 월별(dataCrtYm) 누적 기록 → 최신 달과 약 12개월 전 달의 상세 가입자수 비교
            q = "국민연금 API 사업장 검색(getBassInfoSearchV2)"
            hits, err, info = ctx.nps_api.find(names, biz)
            if hits is None:
                logs.append(search_log(q, "error", "nps", names, err))
                return {"status": {"nps": "failed"}, "search_log": logs}
            if info.get("branches"):
                nps_notes.append(f"국민연금 지점 사업장 {info['branches']}곳이 별도 등록 — 법인 동일성 확인 불가로 "
                                 "본사(법인명 일치) 사업장만 반영, 전체 고용 규모는 과소 추정일 수 있음")
            if info.get("truncated"):
                nps_notes.append(f"국민연금 검색 결과 {info['total']}건 중 일부만 확인(페이지 상한)")
            entities = {(h.get("bzowrRgstNo", "")[:6], norm_name(h.get("wkplNm", ""))) for h in hits}
            if not hits or (len(entities) > 1 and not biz):
                msg = "법인명과 일치하는 사업장 없음" if not hits else f"동명 사업장 {len(entities)}곳 — 사업자등록번호 필요"
                logs.append(search_log(q, "not_found", "nps", names, msg))
                return {"status": {"nps": "not_found"}, "search_log": logs, "notes": nps_notes}
            logs.append(search_log(q, "found", "nps", names))
            months = {h["dataCrtYm"]: h for h in hits if h.get("dataCrtYm") and h.get("seq")}
            if not months:
                logs.append(search_log(q, "error", "nps", names, "자료생성년월·식별번호 없음"))
                return {"status": {"nps": "failed"}, "search_log": logs}
            ym_l = max(months)
            d_l, _ = parse_date(ym_l)
            ent = next(iter(entities))
            entity = f"nps:{ent[0]}:{ent[1]}"

            def observe(ym: str, label: str) -> Optional[int]:
                h = months[ym]
                det, err = ctx.nps_api.detail(h["seq"])
                url = f"https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2#seq={h['seq']}&ym={ym}"
                if not det or not str(det.get("jnngpCnt", "")).strip():
                    logs.append(search_log(f"국민연금 API 상세 {ym}", "error", "nps", names, err or "가입자수 없음"))
                    return None
                cnt = int(float(det["jnngpCnt"]))
                facts.append(fact(ym, cnt, entity, url, f"국민연금 가입 사업장 상세 {ym}",
                                  f"{h.get('wkplNm')} 국민연금 가입자 수 {cnt}명 ({ym}{label})"))
                logs.append(search_log(f"국민연금 API 상세 {ym}", "found", "nps", names, urls=[url]))
                return cnt

            count = observe(ym_l, "")
            gmin, gmax = S.TREND_BASE_GAP_MONTHS
            window = [add_months(d_l, -k).strftime("%Y%m") for k in range(gmin, gmax + 1)] if d_l else []
            base_ym = min((y for y in window if y in months),
                          key=lambda y: abs(int(y[:4]) * 12 + int(y[4:]) - (d_l.year * 12 + d_l.month - 12)),
                          default=None)
            if count is not None and base_ym:
                observe(base_ym, ", 비교 기준")
            elif count is not None and d_l:
                # 가이드: 1년치만 제공 → 12개월 전 기록이 없으면 월별 취득·상실로 역산 (12개월 모두 있어야 사용)
                net = []
                for k in range(12):
                    ymk = add_months(d_l, -k).strftime("%Y%m")
                    if ymk not in months:
                        break
                    f, err = ctx.nps_api.monthly_flow(months[ymk]["seq"], ymk)
                    if not f or not str(f.get("nwAcqzrCnt", "")).strip():
                        break
                    net.append(int(float(f["nwAcqzrCnt"])) - int(float(f.get("lssJnngpCnt") or 0)))
                if len(net) == 12:
                    ymb = add_months(d_l, -12).strftime("%Y%m")
                    base = count - sum(net)
                    url = f"https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2#flow={ymb}"
                    facts.append(fact(ymb, base, entity, url, f"국민연금 월별 취득·상실 역산 {ymb}",
                                      f"{months[ym_l].get('wkplNm')} 국민연금 가입자 수 {base}명 "
                                      f"({ymb}, 이후 12개월 취득·상실 역산)"))
                    logs.append(search_log("국민연금 API 12개월 취득·상실", "found", "nps", names, urls=[url]))
                else:
                    logs.append(search_log("국민연금 API 12개월 취득·상실", "not_found", "nps", names,
                                           "약 12개월 전 기록·월별 흐름 미확보"))
        else:
            logs.append(search_log("국민연금", "error", "nps", names, "data/nps 스냅샷·DATA_GO_KR_API_KEY 없음"))
            return {"status": {"nps": "not_reviewed"}, "search_log": logs}
        return {"status": {"nps": "ok" if facts else "not_found"}, "search_log": logs, "facts": facts, "notes": nps_notes}
    return node


# ─────────────────────────────────────────────
# LLM 추출 노드
# ─────────────────────────────────────────────
def _unit_multiplier(unit: Optional[str]) -> Optional[int]:
    u = (unit or "").replace(" ", "").lower()
    for k, v in UNIT_WORDS.items():
        if u.endswith(k):
            return v
    return None


def item_to_fact(it, doc: dict, as_of: Optional[date] = None) -> Optional[dict]:
    independent = doc["source_type"] in ("official", "partner") or \
        (doc["source_type"] == "news" and not it.is_press_release_copy)
    verified = excerpt_in_doc(it, doc)
    status = "confirmed" if independent and verified else ("partial" if independent else "unverified")
    if status == "confirmed" and not doc.get("published_at") and doc["source_type"] != "official":
        # 게재일 미상: 기준일 이전(당일 포함)에 받은 결과면 기준일 당시 존재가 확인됨 → 인정.
        # 기준일보다 나중에 받은 결과(과거 기준일로 다시 돌리는 경우)는 존재 확인 불가 → partial (schema.Source 규칙)
        if not (as_of and doc.get("retrieved_at") and doc["retrieved_at"] <= as_of.isoformat()):
            status = "partial"
    src = {k: doc.get(k) for k in ("url", "title", "publisher", "source_type", "published_at")}
    f = {"source": src, "statement": it.statement, "excerpt": it.excerpt if verified else None,
         "locator": None, "event_date": it.event_date, "status": status, "date_uncertainty": None}
    ev_date, unc = it.event_date, None
    if not ev_date:
        ev_date, unc = doc.get("published_at"), "사건일 미기재 → 게재일로 대체"

    if it.kind == "contract":
        f.update(kind="contract", event_date=ev_date, date_uncertainty=unc, record={
            "product_id": None, "counterparty_id": None, "counterparty_name": it.counterparty_name or "미상",
            "event_date": ev_date, "contract_status": it.contract_status.value if it.contract_status else "unknown",
            "is_paid": it.is_paid, "independent_confirmation": independent})
    elif it.kind == "activity":
        cp = it.counterparty_name
        f.update(kind="activity", event_date=ev_date, date_uncertainty=unc, record={
            "type": it.activity_type.value if it.activity_type else "other", "product_id": None,
            "counterparty_id": f"org:{norm_name(cp)}" if cp else None, "date": ev_date})
    elif it.kind == "revenue":
        mult = _unit_multiplier(it.unit)
        if it.fiscal_year is None or it.value is None or mult is None:
            return None
        f.update(kind="revenue", event_date=f"{it.fiscal_year}-12-31", record={
            "period_start": f"{it.fiscal_year}-01-01", "period_end": f"{it.fiscal_year}-12-31",
            "fiscal_year": it.fiscal_year, "value": float(it.value) * mult, "currency": "KRW",
            "unit": it.unit or "KRW", "accounting_scope": "unknown", "product_id": None})
    elif it.kind == "customer":
        if it.value is None or not ev_date:
            return None
        f.update(kind="customer", event_date=ev_date, date_uncertainty=unc, record={
            "observation_date": ev_date, "customer_ids": [], "count": int(it.value),
            "metric_definition": "기사상 도입 기관·고객 수" + (" (유료)" if it.is_paid else ""),
            "is_paid": it.is_paid})
    else:
        return None
    return f


def make_extract(ctx: Ctx):
    def node(state: TGState) -> dict:
        docs = state.get("docs") or []
        if not docs:
            return {"status": {"llm": "skipped"}}
        if ctx.llm is None:
            return {"status": {"llm": "not_reviewed"},
                    "notes": [f"TRACTION_LLM_MODEL 미설정 → 문서 {len(docs)}건 근거 추출 생략"]}
        docs = [{**d, "doc_id": f"d{i + 1:03d}"} for i, d in enumerate(docs)]
        pairs, errs = extract_items(docs, ctx.names, ctx.llm)
        facts = [f for it, doc in pairs if (f := item_to_fact(it, doc, ctx.as_of))]
        status = "failed" if errs and not pairs else ("partial" if errs else "ok")
        return {"facts": facts, "status": {"llm": status}, "notes": [f"LLM 추출 오류: {e}" for e in errs]}
    return node


# ─────────────────────────────────────────────
# 근거 조립 (Source → Evidence → records, ID 부여·중복 병합)
# ─────────────────────────────────────────────
def _max_num(ids, prefix: str) -> int:
    n = 0
    for i in ids:
        m = re.search(rf":{prefix}(\d+)$", i or "")
        if m:
            n = max(n, int(m.group(1)))
    return n


class Builder:
    def __init__(self, cid: str, prev: Optional[dict]):
        prev = prev or {}
        self.cid = cid
        self.sources = [dict(s) for s in prev.get("sources", [])]
        self.evidence = [dict(e) for e in prev.get("evidence", [])]
        pdata = prev.get("data", {})
        self.records = {k: [dict(r) for r in pdata.get(k, [])] for k in RECORD_KEYS}
        self.url2src = {s["url"]: s["source_id"] for s in self.sources}
        self.n_src = _max_num(self.url2src.values(), "src")
        self.n_ev = _max_num([e["evidence_id"] for e in self.evidence], "ev")
        self._ev_key = {(e["statement"], tuple(e["source_ids"])): e["evidence_id"] for e in self.evidence}
        self.new_sources: list[str] = []
        self.new_evidence: list[str] = []
        self.notes: list[str] = []
        self.unknown_mismatch = 0

    def _source(self, s: dict) -> str:
        if s["url"] in self.url2src:
            return self.url2src[s["url"]]
        self.n_src += 1
        sid = f"{self.cid}:traction:src{self.n_src:02d}"
        self.sources.append({"source_id": sid, "title": s.get("title") or s["url"], "url": s["url"],
                             "publisher": s.get("publisher") or host_of(s["url"]) or "unknown",
                             "source_type": s.get("source_type") or "other", "published_at": s.get("published_at"),
                             "checked_at": date.today().isoformat(), "original_source_id": None})
        self.url2src[s["url"]] = sid
        self.new_sources.append(sid)
        return sid

    def _evidence(self, f: dict, sid: str) -> str:
        key = (f["statement"], (sid,))
        if key in self._ev_key:
            return self._ev_key[key]
        self.n_ev += 1
        eid = f"{self.cid}:traction:ev{self.n_ev:02d}"
        self.evidence.append({"evidence_id": eid, "company_id": self.cid, "product_id": None,
                              "statement": f["statement"], "source_ids": [sid], "locator": f.get("locator"),
                              "excerpt": f.get("excerpt"), "event_date": f.get("event_date"), "geography": None,
                              "evidence_status": f["status"], "date_uncertainty": f.get("date_uncertainty")})
        self._ev_key[key] = eid
        self.new_evidence.append(eid)
        return eid

    @staticmethod
    def _key(kind: str, r: dict):
        month = (lambda d: (d or "")[:7])
        if kind == "revenue":
            return (r.get("fiscal_year"), r.get("accounting_scope"), r.get("product_id"))
        if kind == "contract":
            # 유료 여부·독립 확인이 다른 기사끼리 합치면 '회사 주장 유료' + '독립 확인'이 섞여 B가 부풀려짐
            n = norm_name(r.get("counterparty_name") or "")
            return None if not n or n == "미상" else (n, month(r.get("event_date")), r.get("is_paid"),
                                                     bool(r.get("independent_confirmation")))
        if kind == "activity":
            return None if not r.get("counterparty_id") else (r.get("type"), r["counterparty_id"], month(r.get("date")))
        if kind == "customer":
            return (r.get("observation_date"), r.get("metric_definition"), r.get("count"))
        return (r.get("observation_date"), r.get("entity_id"))

    def add(self, f: dict) -> None:
        eid = self._evidence(f, self._source(f["source"]))
        kind, rec = f["kind"], dict(f["record"])
        store = self.records[f"{kind}_records"]
        key = self._key(kind, rec)
        if key is not None:
            for r in store:
                if self._key(kind, r) != key:
                    continue
                if eid not in r["evidence_ids"]:
                    r["evidence_ids"].append(eid)
                if kind == "contract":        # 키가 같으면 is_paid·independent도 같음 → 상태만 갱신
                    if rec["contract_status"] in EXCLUDED_CONTRACT or r.get("contract_status") in (None, "unknown"):
                        r["contract_status"] = rec["contract_status"]
                if kind == "revenue" and r.get("value") != rec.get("value") and rec.get("accounting_scope") == "unknown":
                    self.unknown_mismatch += 1
                elif kind == "revenue" and r.get("value") != rec.get("value"):
                    self.notes.append(f"{rec['fiscal_year']}년 매출({rec.get('accounting_scope')}) 출처 간 값 불일치 "
                                      f"({r['value']:,.0f} vs {rec['value']:,.0f}) — 먼저 들어온 값 유지, 정의·정정 여부 확인")
                return
        rec["evidence_ids"] = [eid]
        n = len(store) + 1
        if kind == "contract":
            rec["contract_id"] = f"ct{n:03d}"
            if rec["counterparty_name"] != "미상":
                rec["counterparty_id"] = f"org:{norm_name(rec['counterparty_name'])}"
        if kind == "activity":
            rec["activity_id"] = f"ac{n:03d}"
        store.append(rec)


# ─────────────────────────────────────────────
# 판정 노드: 규칙 적용 → Envelope
# ─────────────────────────────────────────────
def assemble(ctx: Ctx, state: dict) -> dict:
    prev = ctx.previous if ctx.mode == "review" else None
    cid, as_of = ctx.company_id, ctx.as_of
    b = Builder(cid, prev)
    for f in state.get("facts") or []:
        b.add(f)

    # 검색 로그: 내부 URL → source_ids
    logs = [dict(x) for x in (prev or {}).get("data", {}).get("search_log", [])]
    new_logs = []
    for x in state.get("search_log") or []:
        x = dict(x)
        urls = x.pop("_urls", [])
        x["source_ids"] = [b.url2src[u] for u in urls if u in b.url2src]
        new_logs.append(x)
    logs += new_logs

    st = dict(state.get("status") or {})
    prev_data = (prev or {}).get("data", {})
    prev_cov = {c["scope"]: c["status"] for c in (prev or {}).get("coverage", [])}
    fds = st.get("disclosure") or prev_data.get("financial_disclosure_status") or "not_reviewed"

    # 뉴스·LLM 경로 상태: ok(전부 성공) / partial(일부 실패) / failed / not_reviewed
    #  → RF1과 none_reason은 ok일 때만 "찾아봤는데 없음"으로 판정 (일부 실패면 조사 미완료)
    if "news" in st:
        news_state = {"ok": "ok", "partial": "partial", "failed": "failed"}.get(st["news"], "not_reviewed")
        if news_state in ("ok", "partial") and st.get("llm") in ("not_reviewed", "failed", "partial"):
            news_state = {"not_reviewed": "not_reviewed", "failed": "failed"}.get(st["llm"], "partial")
    else:  # 보완 라운드에서 다시 돌리지 않음 → 이전 상태 (coverage.note 에 기록해 둔 값)
        prev_note = next((c.get("note", "") for c in (prev or {}).get("coverage", []) if c["scope"] == "contract"), "")
        m = re.search(r"news_state=(\w+)", prev_note)
        pc = prev_cov.get("contract", "not_reviewed")
        news_state = m.group(1) if m else ("failed" if pc == "search_failed" else
                                           "not_reviewed" if pc == "not_reviewed" else "ok")
    nps_state = st.get("nps") or {"search_failed": "failed", "not_reviewed": "not_reviewed"}.get(
        prev_cov.get("headcount", "not_reviewed"), "ok")

    rec = b.records
    confirmed = {e["evidence_id"] for e in b.evidence if e["evidence_status"] == "confirmed"}
    ok = (lambda r: any(i in confirmed for i in r.get("evidence_ids", [])))

    # ── 단계
    stage, stage_ids, stage_notes = commercial_stage(as_of, rec["revenue_records"], rec["contract_records"],
                                                       rec["activity_records"], confirmed)
    none_reason = None
    if stage == "none":
        incomplete = news_state != "ok" or fds in ("search_failed", "not_reviewed")
        none_reason = "search_incomplete" if incomplete else "no_recognized_evidence"
    recognized = True if stage in ("A", "B") else False if stage in ("C", "D") else None

    # ── 성장
    by_year, scope = revenue_by_year(rec["revenue_records"], confirmed, as_of)
    # 감사보고서별 당기·전기 값 (재작성 판별용) — DART 근거 문장에서 복원 (보완 라운드에서도 동일)
    ev_by_id = {e["evidence_id"]: e for e in b.evidence}
    src_pub = {x["source_id"]: x.get("published_at") for x in b.sources}
    report_values: dict[str, dict[int, float]] = {}
    for r in rec["revenue_records"]:
        if r.get("accounting_scope") != scope or r.get("product_id"):
            continue
        for eid in r.get("evidence_ids", []):
            e = ev_by_id.get(eid)
            m = re.search(r"(\d{4})년 (-?[\d,]+)원 \((당기|전기)\)", (e or {}).get("statement", ""))
            if e and m and e["evidence_status"] == "confirmed":
                sid = e["source_ids"][0]
                rkey = f"{src_pub.get(sid) or ''}|{sid}"          # 접수일|source_id → 최근 보고서 판별
                report_values.setdefault(rkey, {})[int(m.group(1))] = float(m.group(2).replace(",", ""))
    rate, tier, gdetail = cagr(by_year, report_values)
    growth_ids = [i for v in by_year.values() for i in v["evidence_ids"]]

    # ── 추이
    hc_obs = [(h["observation_date"], h["count"], f"{h['population']}|{h['entity_id']}", h["evidence_ids"])
              for h in rec["headcount_records"] if ok(h)]
    hc_trend, hc_detail = trend(hc_obs, as_of)
    cu_obs = [(c["observation_date"], len(c["customer_ids"]) if c.get("customer_ids") else c.get("count"),
               f"{c['metric_definition']}|{c.get('is_paid')}", c["evidence_ids"])
              for c in rec["customer_records"] if ok(c)]
    cu_trend, cu_detail = trend(cu_obs, as_of)

    # ── 기대치 · 레드플래그 · C5
    exp, req = expectation(ctx.invest_stage, stage, none_reason)
    flags, rf_notes = red_flags(as_of, rec["contract_records"], rec["activity_records"], confirmed, tier, rate,
                                  growth_ids, hc_detail, exp, ctx.invest_stage, stage, req,
                                  contract_search_ok=(news_state == "ok"))
    c5_in = c5(stage, tier, stage_ids + growth_ids)

    # ── findings
    fid = (lambda name: f"{cid}:traction:f-{name}")
    findings = []

    def add_f(name, category, claim, ids, unc=None):
        findings.append({"finding_id": fid(name), "category": category, "claim": claim,
                         "evidence_ids": sorted(set(ids)), "uncertainty": unc or None, "related_criterion_ids": ["C5"]})

    add_f("stage", "stage", f"상업화 단계 {stage}" + (f" ({none_reason})" if none_reason else "")
          + f" — 최근 {S.STAGE_WINDOW_MONTHS}개월 인정 근거 기준", stage_ids, "; ".join(stage_notes))
    add_f("disclosure", "disclosure", f"DART 감사보고서 조회 결과 {fds}"
          + (f", {scope} 매출 {len(by_year)}개 연도" if by_year else ""), growth_ids)
    add_f("growth", "growth",
          f"매출 CAGR {rate:.1%} ({gdetail['observed_years'][0]}→{gdetail['observed_years'][-1]}, {scope}) → {tier}"
          if rate is not None else f"성장 구간 G0: {gdetail['unavailable_reason']}", growth_ids)
    for name, tr, dt in (("headcount", hc_trend, hc_detail), ("customer", cu_trend, cu_detail)):
        label = {"headcount": "고용", "customer": "고객"}[name]
        claim = (f"{label} 추이 {tr} ({dt['change_rate']:+.1%}, {dt['period_start']}→{dt['period_end']})"
                 if dt.get("change_rate") is not None else f"{label} 추이 {tr}: {dt.get('unavailable_reason')}")
        add_f(name, name, claim, dt.get("evidence_ids", []))
    add_f("expectation", "expectation",
          f"투자 단계 {ctx.invest_stage or '미상'} → 최소 기대 {req or '미정'}, 상업화 {stage} → {exp}", stage_ids)
    add_f("c5", "c5", f"C5 = {c5_in['score']} ({c5_in['score_status']}) — {c5_in['rationale']}", c5_in["evidence_ids"])
    red = []
    for fl in flags:
        add_f(fl["code"].lower(), "red_flag", f"{fl['code']}: {fl['reason']}", fl["evidence_ids"])
        red.append({**fl, "finding_id": fid(fl["code"].lower())})

    # ── coverage
    def cov_news(kind: str) -> str:
        rs = rec[f"{kind}_records"]
        if any(ok(r) for r in rs):
            return "reviewed"
        if news_state == "failed":
            return "search_failed"
        if news_state == "not_reviewed":
            return "not_reviewed"
        if news_state == "partial":
            return "insufficient_information"
        return "insufficient_information" if rs else "no_relevant_evidence_found"

    fds_cov = {"found": "reviewed", "not_found": "no_relevant_evidence_found",
               "search_failed": "search_failed", "not_reviewed": "not_reviewed"}[fds]
    cov = {
        "disclosure": fds_cov,
        "revenue": "reviewed" if by_year else ("insufficient_information" if fds == "found" else fds_cov),
        "contract": cov_news("contract"), "activity": cov_news("activity"), "customer": cov_news("customer"),
        "headcount": "reviewed" if hc_obs else {"failed": "search_failed", "not_reviewed": "not_reviewed",
                                                "not_found": "no_relevant_evidence_found"}.get(nps_state,
                                                                                         "insufficient_information"),
    }
    tools = {"disclosure": {"dart"}, "revenue": {"dart"}, "headcount": {"nps"},
             "contract": {"naver", "tavily", "news"}, "activity": {"naver", "tavily", "news"},
             "customer": {"naver", "tavily", "news"}}
    coverage = [{"scope": k, "status": v, "note": f"news_state={news_state}" if k in ("contract", "activity", "customer") else "",
                 "search_log_indices": [i for i, x in enumerate(logs) if log_tool(x) in tools[k]]}
                for k, v in cov.items()]
    core = [cov["disclosure"], cov["contract"], cov["headcount"]]
    bad = [c for c in core if c in ("search_failed", "not_reviewed")]
    incomplete = bad or news_state == "partial" or st.get("nps") == "failed"
    analysis_status = "failed" if len(bad) == len(core) else "partial" if incomplete else "complete"

    # ── missing_items
    missing = []

    def add_m(field_, cause, route, impact, detail=""):
        missing.append({"item_id": f"{cid}:traction:m{len(missing) + 1:02d}", "field": field_, "cause": cause,
                        "route": route, "impact": impact, "detail": detail or ""})

    cause_of = {"search_failed": "search_failed", "not_reviewed": "not_reviewed"}
    if not ctx.invest_stage:
        add_m("invest_stage", "input_missing", "profile", "단계 기대치·RF4 판단 불가")
    if tier == "G0":
        add_m("revenue_by_year", cause_of.get(fds_cov, "not_found"), "due_diligence",
              "성장 구간 G0 → C5 성장 점수 없음", gdetail["unavailable_reason"])
    if hc_trend == "unknown":
        add_m("headcount_records", cause_of.get(cov["headcount"], "not_found"), "due_diligence",
              "고용 추이·RF2(고용) 판단 불가", hc_detail.get("unavailable_reason"))
    if cu_trend == "unknown":
        add_m("customer_records", cause_of.get(cov["customer"], "not_found"), "due_diligence",
              "고객 추이 판단 불가", cu_detail.get("unavailable_reason"))
    if news_state != "ok":
        add_m("contract_records", "search_failed" if news_state == "failed" else "not_reviewed", "traction",
              "단계 B·C·D, RF1·RF3 판단 불완전", "뉴스 검색 실패 또는 API 키·LLM 미설정")

    # ── open_questions
    restated = set(restated_years(sorted(by_year), report_values))
    dart_notes = [x for x in b.notes if not any(x.startswith(f"{y}년 매출({scope})") for y in restated)]
    oq = list(dict.fromkeys(stage_notes + rf_notes + dart_notes + (state.get("notes") or [])))
    if b.unknown_mismatch:
        oq.append(f"기사상 매출(회계 범위 불명) 출처 간 불일치 {b.unknown_mismatch}건 — 성장률 계산에는 미사용")
    if restated:
        oq.append(f"{sorted(restated)}년 매출이 감사보고서마다 다름(정정·연결범위 변경) → 성장률 G0 처리. "
                  "변경 사유와 같은 기준의 3개년 매출 확인 필요")
    if stage == "B" and not by_year:
        oq.append("유료 계약은 확인됐으나 연도별 매출 미확인 — 매출 규모 확인 필요")
    if by_year and max(by_year) < as_of.year - 1:
        oq.append(f"최신 감사보고서 매출이 {max(by_year)}년 — 외부감사 대상 제외 또는 제출 지연 여부 확인")
    ys = sorted(by_year)
    if len(ys) >= 2 and rate is not None and ys[-1] - 1 in by_year and by_year[ys[-2]]["value"] > 0:
        yoy = by_year[ys[-1]]["value"] / by_year[ys[-2]]["value"] - 1
        if yoy < 0 <= rate:
            oq.append(f"CAGR {tier}이지만 {ys[-1]}년 매출이 전년 대비 {yoy:.0%} — 최근 추세 확인 필요")
    claim_only = [c for c in rec["contract_records"] if c.get("is_paid") and not c.get("independent_confirmation")]
    if claim_only:
        oq.append(f"회사 발표로만 확인된 유료 계약 {len(claim_only)}건 — 거래 상대 확인 필요")

    data = {
        "invest_stage": ctx.invest_stage, "invest_stage_observed_at": ctx.profile.get("invest_stage_date"),
        "financial_disclosure_status": fds, "commercial_stage": stage, "none_reason": none_reason,
        "traction_recognized": recognized, "revenue_by_year": by_year, "revenue_cagr": rate, "growth_tier": tier,
        "growth_detail": gdetail, "headcount_trend": hc_trend, "headcount_detail": hc_detail,
        "customer_trend": cu_trend, "customer_detail": cu_detail, "expectation_status": exp, "red_flags": red,
        "criteria_inputs": {"C5": c5_in}, **rec, "search_log": logs, "open_questions": oq,
    }
    env = {
        "run_id": (prev or {}).get("run_id") or ctx.run_id, "company_id": cid, "as_of": as_of.isoformat(),
        "schema_version": S.SCHEMA_VERSION, "criteria_version": S.CRITERIA_VERSION, "agent": S.AGENT_NAME,
        "result_version": ((prev or {}).get("result_version") or 0) + 1, "analysis_status": analysis_status,
        "data": data, "sources": b.sources, "evidence": b.evidence, "findings": findings,
        "missing_items": missing, "coverage": coverage,
        "review_responses": list((prev or {}).get("review_responses", [])),
    }
    if ctx.mode == "review":
        env["review_responses"].append(review_response(ctx, prev, findings, b, new_logs, env["result_version"]))
    return S.AnalysisEnvelope.model_validate(env).model_dump(mode="json")


def review_response(ctx: Ctx, prev: dict, findings: list, b: Builder, new_logs: list, version: int) -> dict:
    req = ctx.request
    before = {f["finding_id"]: f["claim"] for f in prev.get("findings", [])}
    after = {f["finding_id"]: f["claim"] for f in findings}
    targets = req.get("finding_ids") or list(after)
    changed = [k for k in after if before.get(k) != after[k]] + [k for k in before if k not in after]
    target_changed = [k for k in targets if k in changed]
    all_failed = bool(new_logs) and all(x["status"] == "error" for x in new_logs)
    facts = bool(b.new_evidence)
    change_type = ("both" if facts and changed else "facts_updated" if facts
                   else "interpretation_updated" if changed else "unchanged")
    qres = []
    for q in req.get("questions", []):
        if target_changed:
            s = "answered"
            ans = "; ".join(f"{before.get(k, '(없음)')} → {after.get(k, '(해제)')}" for k in target_changed)
        elif facts:
            s = "partially_answered"
            ans = f"새 근거 {len(b.new_evidence)}건 확보, 판단 유지: " + "; ".join(after.get(k, k) for k in targets)
        elif all_failed:
            s, ans = "unanswered", "추가 검색 실패"
        else:
            s, ans = "unanswered", "추가 근거 미발견 — 기존 판단이 옳다는 증명은 아님"
        qres.append({"question_id": q["question_id"], "status": s, "answer": ans, "evidence_ids": b.new_evidence[:20]})
    ss = {x["status"] for x in qres}
    if qres and ss == {"answered"}:
        resolution = "resolved"
    elif ss & {"answered", "partially_answered"}:
        resolution = "partially_resolved"
    elif all_failed:
        resolution = "search_failed"
    elif not qres and target_changed:
        resolution = "resolved"
    else:
        resolution = "unresolved"
    used = sum(b.used for b in ctx.budgets.values())
    return {
        "response_id": f"{req['request_id']}:resp", "request_id": req["request_id"], "company_id": ctx.company_id,
        "result_version": version, "resolution": resolution, "change_type": change_type,
        "updated_finding_ids": changed, "new_source_ids": b.new_sources, "question_results": qres,
        "remaining_unknowns": [q["text"] for q, r in zip(req.get("questions", []), qres) if r["status"] != "answered"],
        "limitations": [f"검색 예산 {req.get('search_budget', {}).get('max_calls', S.REVIEW_MAX_CALLS)}회 중 {used}회 사용",
                        "뉴스는 검색 API 요약문만 사용(기사 본문 미수집)"],
    }


def make_judge(ctx: Ctx):
    def node(state: TGState) -> dict:
        return {"envelope": assemble(ctx, state)}
    return node


def build_graph(ctx: Ctx):
    g = StateGraph(TGState)
    g.add_node("collect_dart", make_collect_dart(ctx))
    g.add_node("collect_news", make_collect_news(ctx))
    g.add_node("collect_nps", make_collect_nps(ctx))
    g.add_node("extract", make_extract(ctx))
    g.add_node("judge", make_judge(ctx))
    for n in ("collect_dart", "collect_news", "collect_nps"):
        g.add_edge(START, n)
    g.add_edge(["collect_dart", "collect_news"], "extract")   # DART 파싱 실패 원문도 LLM으로
    g.add_edge(["extract", "collect_nps"], "judge")
    g.add_edge("judge", END)
    return g.compile()


# ─────────────────────────────────────────────
# 실행 진입점
# ─────────────────────────────────────────────
def _initial_queries(names: list[str]) -> list[tuple[str, str]]:
    q = []
    for n in names[:2]:
        q += [("naver", f"{n} {s}") for s in NEWS_QUERY_SUFFIX]
        q.append(("tavily", f"{n} 의료 AI 병원 공급 계약 매출"))
    return q


def _review_plan(ctx: Ctx, related: dict) -> None:
    req, prev = ctx.request, ctx.previous
    cats = {f.rsplit(":f-", 1)[-1] for f in req.get("finding_ids", [])}
    prefer = set(req.get("context", {}).get("preferred_sources") or [])
    ctx.run_news = not cats or bool(cats & NEWS_CATS) or bool(prefer & {"naver", "tavily", "news"})
    # DART: 최초 실행과 다른 방법 → 별도·연결 모두, 연도 확대
    ctx.run_dart = bool(cats & DART_CATS) or "dart" in prefer
    ctx.dart_both_scopes, ctx.dart_reports_per_scope = True, 3
    ctx.run_nps = False                              # 대체 조회 경로 없음 (limitations에 기록)

    used = {log_query(x) for x in prev.get("data", {}).get("search_log", [])}
    used |= {log_query(x) for x in req.get("context", {}).get("previous_search_history", [])}
    clues: list[str] = []
    for p in ctx.profile.get("products") or []:
        if p.get("name") or p.get("product_name"):
            clues.append(p.get("name") or p.get("product_name"))
    _clue_strings(related, clues)
    clues = [c for c in dict.fromkeys(clues) if norm_name(c) not in {norm_name(n) for n in ctx.names}][:5]

    n0 = ctx.names[0]
    qs = [("tavily", f"{n0} {q['text']}") for q in req.get("questions", [])]
    qs += [("naver", f"{n0} {c} 계약") for c in clues]
    qs += [("naver", f"{n0} {s}") for s in REVIEW_SYNONYMS]
    total = (req.get("search_budget") or {}).get("max_calls", S.REVIEW_MAX_CALLS)
    dart_share = min(2, total) if ctx.run_dart else 0
    ctx.budgets["dart"].max_calls, ctx.budgets["nps"].max_calls = dart_share, 0
    ctx.budgets["news"].max_calls = total - dart_share
    ctx.news_queries = [q for q in qs if q[1] not in used][:max(0, total - dart_share)]
    if not ctx.news_queries:
        ctx.run_news = False          # 새 검색어가 없으면 뉴스 범위는 이전 상태 유지
    ctx.prev_urls = {s["url"] for s in prev.get("sources", [])}


def run_traction(profile: dict, *, as_of: Optional[str] = None, run_id: Optional[str] = None,
                 request: Optional[dict] = None, previous: Optional[dict] = None,
                 related: Optional[dict] = None, llm: Any = "auto", clients: Optional[dict] = None) -> dict:
    """한 기업 분석. request가 있으면 보완 라운드(previous 필수). 반환: AnalysisEnvelope dict."""
    load_env()
    if request and not previous:
        raise ValueError("보완 요청에는 이전 결과(previous)가 필요합니다")
    as_of_s = (request or {}).get("as_of") or (previous or {}).get("as_of") or as_of or date.today().isoformat()
    as_of_d, _ = parse_date(as_of_s)
    names = company_names(profile)
    if not names:
        raise ValueError("company_profile에 기업명이 없습니다 (legal_name/name/기업명)")

    # 노드별 예산: 최초 실행은 S.BUDGET_SPLIT, 보완 라운드는 _review_plan 에서 요청 예산을 나눔
    budgets = {k: Budget(v) for k, v in S.BUDGET_SPLIT.items()}
    http = {k: Http(b) for k, b in budgets.items()}
    env = os.environ
    c = clients or {}
    ctx = Ctx(
        profile=profile, as_of=as_of_d, run_id=run_id or uuid.uuid4().hex[:12], company_id=company_id_of(profile),
        names=names, invest_stage=profile.get("invest_stage") or profile.get("최근 투자단계"),
        homepage_host=host_of(profile.get("homepage") or ""), budgets=budgets,
        dart=c.get("dart", DartClient(env["DART_API_KEY"], http["dart"]) if env.get("DART_API_KEY") else None),
        naver=c.get("naver", NaverNews(env["NAVER_CLIENT_ID"], env["NAVER_CLIENT_SECRET"], http["news"])
                    if env.get("NAVER_CLIENT_ID") and env.get("NAVER_CLIENT_SECRET") else None),
        tavily=c.get("tavily", Tavily(env["TAVILY_API_KEY"], http["news"]) if env.get("TAVILY_API_KEY") else None),
        nps_files=c.get("nps_files", NpsSnapshots(Path(env.get("NPS_SNAPSHOT_DIR", ROOT / "data" / "nps")))),
        nps_api=c.get("nps_api", NpsApi(env["DATA_GO_KR_API_KEY"], http["nps"])
                      if env.get("DATA_GO_KR_API_KEY") else None),
        llm=get_llm() if llm == "auto" else llm,
    )
    if request:
        ctx.mode, ctx.request, ctx.previous = "review", request, previous
        _review_plan(ctx, related or {})
    else:
        ctx.news_queries = _initial_queries(names)
    if ctx.naver is None and ctx.tavily is not None:
        # 네이버 키가 없으면(API HUB 이관·사업자 회원 필요) 같은 검색어를 Tavily로 보낸다
        ctx.news_queries = list(dict.fromkeys(("tavily", q) for _, q in ctx.news_queries))
    return build_graph(ctx).invoke({})["envelope"]


def traction_growth_node(state: dict) -> dict:
    """메인 그래프 노드. 보완 요청이 없고 결과가 이미 있으면 아무것도 바꾸지 않는다."""
    profile = state.get("company_profile") or state.get("current_company") or {}
    cid = company_id_of(profile)
    prev = state.get("traction_analysis")
    if prev and prev.get("company_id") != cid:
        prev = None
    reqs = [r for r in state.get("review_requests") or []
            if r.get("target_agent") == "traction" and r.get("company_id") == cid]
    related = {k: state.get(k) for k in ("clinical_analysis", "risk_analysis") if state.get(k)}

    if not reqs:
        if prev:
            return {}
        return {"traction_analysis": run_traction(profile, as_of=state.get("as_of"), run_id=state.get("run_id"))}

    env = prev or run_traction(profile, as_of=state.get("as_of"), run_id=state.get("run_id"))
    done = {x["request_id"] for x in env.get("review_responses", [])}
    for r in reqs:
        if r.get("review_round", 1) > S.MAX_REVIEW_ROUNDS or r["request_id"] in done:
            continue
        env = run_traction(profile, request=r, previous=env, related=related)
    return {"traction_analysis": env}


def _summary(env: dict) -> str:
    d = env["data"]
    lines = [f"[{env['company_id']}] as_of={env['as_of']} status={env['analysis_status']} v{env['result_version']}",
             f"  공시={d['financial_disclosure_status']}  단계={d['commercial_stage']}"
             f"{' (' + d['none_reason'] + ')' if d['none_reason'] else ''}  기대치={d['expectation_status']}",
             f"  매출={ {y: v['value'] for y, v in d['revenue_by_year'].items()} }",
             f"  CAGR={d['revenue_cagr']}  성장={d['growth_tier']}  고용={d['headcount_trend']}  고객={d['customer_trend']}",
             f"  RF={[r['code'] + ':' + r['status'] for r in d['red_flags']]}  C5={d['criteria_inputs']['C5']['score']}",
             f"  근거 {len(env['evidence'])}건 · 출처 {len(env['sources'])}건 · 검색 {len(d['search_log'])}회"]
    lines += [f"  ? {q}" for q in d["open_questions"]]
    return "\n".join(lines)


def _save(env: dict, name: str) -> Path:
    out = CACHE_DIR / "traction" / f"{env['company_id']}_{norm_name(name)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def run_all(as_of: Optional[str], llm: Any, csv_path: Path = ROOT / "data" / "startup_list.csv") -> Path:
    """CSV 전체(중복 기업명 제외)를 기업 5곳씩 동시에 실행 → 기업별 JSON + summary.csv."""
    with open(csv_path, encoding="utf-8-sig") as f:
        names = list(dict.fromkeys(r["기업명"] for r in csv.DictReader(f) if r.get("기업명")))
    load_env()
    shared_llm = get_llm() if llm == "auto" else llm        # 모델 객체 1개를 기업들이 공유

    def one(name: str):
        t = time.perf_counter()
        try:
            env = run_traction(profile_from_csv(name, csv_path) or {"legal_name": name}, as_of=as_of, llm=shared_llm)
            _save(env, name)
            return name, env, None, time.perf_counter() - t
        except Exception as e:
            return name, None, f"{type(e).__name__}: {str(e)[:200]}", time.perf_counter() - t

    rows = []
    with ThreadPoolExecutor(max_workers=S.RUNTIME_DEFAULTS["company_concurrency"]) as ex:
        for i, (name, env, err, sec) in enumerate(ex.map(one, names), start=1):
            if env is None:
                print(f"[{i}/{len(names)}] {name}  실패 {err}")
                rows.append({"기업명": name, "오류": err})
                continue
            d = env["data"]
            print(f"[{i}/{len(names)}] {name}  단계={d['commercial_stage']} 성장={d['growth_tier']} "
                  f"C5={d['criteria_inputs']['C5']['score']} 상태={env['analysis_status']}  {sec:.1f}s")
            rows.append({"기업명": name, "company_id": env["company_id"], "투자단계": d["invest_stage"],
                         "공시": d["financial_disclosure_status"], "단계": d["commercial_stage"],
                         "none_reason": d["none_reason"], "CAGR": d["revenue_cagr"], "성장": d["growth_tier"],
                         "고용": d["headcount_trend"], "기대치": d["expectation_status"],
                         "RF": " ".join(r["code"] for r in d["red_flags"]),
                         "C5": d["criteria_inputs"]["C5"]["score"], "상태": env["analysis_status"],
                         "근거수": len(env["evidence"]), "소요초": round(sec, 1), "오류": ""})
    out = CACHE_DIR / "traction" / "summary.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="실적·성장성 분석 (4번) 단독 실행")
    ap.add_argument("name", nargs="?", help="data/startup_list.csv 의 기업명 (--all이면 생략)")
    ap.add_argument("--all", action="store_true", help="CSV 전체 기업 일괄 실행 (캐시 채우기)")
    ap.add_argument("--as-of", help="기준일 YYYY-MM-DD (기본: 오늘) — 캐시를 재사용하려면 매번 같은 값")
    ap.add_argument("--stage", help="투자 단계 덮어쓰기 (예: 'Series B')")
    ap.add_argument("--no-llm", action="store_true", help="LLM 추출 생략")
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 새로 받아 캐시 갱신")
    ap.add_argument("--no-cache", action="store_true", help="캐시를 읽지도 쓰지도 않음")
    args = ap.parse_args()
    if args.refresh:
        os.environ["TRACTION_CACHE_REFRESH"] = "1"
    if args.no_cache:
        os.environ["TRACTION_CACHE"] = "0"
    llm = None if args.no_llm else "auto"

    if args.all:
        t = time.perf_counter()
        out = run_all(args.as_of, llm)
        print(f"\n요약: {out.relative_to(ROOT)}  (총 {time.perf_counter() - t:.0f}초)")
        return
    if not args.name:
        ap.error("기업명 또는 --all 이 필요합니다")
    profile = profile_from_csv(args.name) or {"legal_name": args.name}
    if args.stage:
        profile["invest_stage"] = args.stage
    t = time.perf_counter()
    env = run_traction(profile, as_of=args.as_of, llm=llm)
    out = _save(env, args.name)
    print(_summary(env))
    print(f"\n전체 결과: {out.relative_to(ROOT)}  ({time.perf_counter() - t:.1f}초)")


if __name__ == "__main__":
    main()
