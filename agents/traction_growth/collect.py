"""실적·성장성 — 수집 (OpenDART · 뉴스(네이버·Tavily) · 국민연금).

Orchestrator 구조에서는 "01 공통 데이터 수집" 노드로 옮겨질 부분이다.
수집 결과는 공통 데이터셋(docs/common_state.md 3장)으로 반환한다 → collect_dataset()

    START ─┬─ collect_dart ─┐
           ├─ collect_news ─┼─ END        (병렬, 판정·LLM 없음)
           └─ collect_nps  ─┘
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Optional
import xml.etree.ElementTree as ET
import zipfile

from langgraph.graph import END, START, StateGraph
import requests

from . import schema as S
from .common import (
    CACHE_DIR,
    Budget,
    Ctx,
    TGState,
    _host_in,
    _source_type,
    add_months,
    cache_on,
    cache_refresh,
    host_of,
    log_query,
    norm_name,
    now_iso,
    parse_date,
    search_log,
)

_SEM = threading.BoundedSemaphore(S.RUNTIME_DEFAULTS["external_call_concurrency"])

SECRET_PARAMS = {"crtfc_key", "serviceKey"}
CACHE_TTL_HOURS = [                 # (URL에 포함된 문자열, 유효 시간) — 먼저 맞는 것 적용, -1 = 영구
    ("/document.xml", -1),          # 접수된 감사보고서 원문은 바뀌지 않음 (정정은 새 접수번호)
    ("opendart.fss.or.kr", 24 * 7),
    ("apis.data.go.kr", 24 * 7),
    ("api.tavily.com", 24 * 7),
    ("openapi.naver.com", 24 * 7),
]


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


NEWS_QUERY_SUFFIX = ["계약", "공급 계약", "도입 병원", "매출", "MOU 협약", "실증 PoC"]
REVIEW_SYNONYMS = ["공급", "납품", "구독", "판매 계약", "수가", "유료 도입"]
NEWS_CATS = {"stage", "rf1", "rf3", "customer", "expectation", "rf4", "c5"}
DART_CATS = {"disclosure", "growth", "rf2", "c5"}
POPULATION_NPS = "국민연금 사업장 가입자 수"


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
# 약관상 자동 수집·AI 활용 금지 (criteria.md 2-2) → 검색 결과에서 제외
EXCLUDED_HOSTS = ("innoforest.co.kr", "thevc.kr")


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


# ─────────────────────────────────────────────
# 수집 전용 실행 → 공통 데이터셋 (Worker 입력의 dataset)
# ─────────────────────────────────────────────
def build_collect_graph(ctx: Ctx):
    """수집 노드 3개만 병렬 실행 (판정·LLM 없음)."""
    g = StateGraph(TGState)
    for name, make in (("collect_dart", make_collect_dart), ("collect_news", make_collect_news),
                       ("collect_nps", make_collect_nps)):
        g.add_node(name, make(ctx))
        g.add_edge(START, name)
        g.add_edge(name, END)
    return g.compile()


def to_dataset(state: dict, company_id: str, version: str) -> dict:
    """수집 State → 공통 데이터셋 {version, sources, records} (docs/common_state.md 3장).

    - 출처 URL 하나 = Source 하나 (source_id: {company_id}:src{n})
    - 구조화 사실(DART 매출·국민연금 고용) → content dict {kind, record, status, statement, excerpt, event_date}
    - 원문(뉴스 요약·DART 파싱 실패 본문) → content 문자열
    - tags: ["traction", 종류, 출처 유형] — 평가 Worker의 레코드 배분·독립 확인 판단에 사용
    - 검색 기록·수집 상태는 계약에 없으므로 notes 와 함께 dataset 밖(로그)으로 남긴다 → collect_log
    """
    sources: dict[str, dict] = {}

    def sid_of(s: dict) -> str:
        key = s.get("url") or s.get("title")
        if key not in sources:
            sources[key] = {"source_id": f"{company_id}:src{len(sources) + 1:03d}", "title": s.get("title"),
                            "url": s.get("url"), "published_at": s.get("published_at"),
                            "collected_at": s.get("retrieved_at") or now_iso()[:10]}
        return sources[key]["source_id"]

    records = []
    for f in state.get("facts") or []:
        records.append({"record_id": f"{company_id}:rec{len(records) + 1:03d}", "source_id": sid_of(f["source"]),
                        "locator": f.get("locator"),
                        "tags": ["traction", f["kind"], f["source"].get("source_type") or "other"],
                        "content": {k: f.get(k) for k in ("kind", "record", "status", "statement", "excerpt",
                                                          "event_date", "date_uncertainty")}})
    for d in state.get("docs") or []:
        text = f"{d.get('title') or ''}\n{d.get('text') or ''}".strip()
        records.append({"record_id": f"{company_id}:rec{len(records) + 1:03d}", "source_id": sid_of(d),
                        "locator": None, "tags": ["traction", d.get("tool") or "news", d.get("source_type") or "other"],
                        "content": text})
    return {"version": version, "sources": list(sources.values()), "records": records}


def collect_dataset(ctx: Ctx, version: str) -> tuple[dict, dict]:
    """설정된 Ctx(클라이언트·검색어·예산)로 수집만 실행 → (공통 데이터셋, 수집 로그).
    Ctx 구성은 agent.make_ctx() 참고."""
    state = build_collect_graph(ctx).invoke({})
    log = {"status": dict(state.get("status") or {}), "search_log": list(state.get("search_log") or []),
           "notes": list(state.get("notes") or [])}
    return to_dataset(state, ctx.company_id, version), log
