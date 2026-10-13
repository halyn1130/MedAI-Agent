"""실적·성장성 — 공통 도구 (수집·평가 양쪽에서 쓰는 것만).

환경변수·이름 정규화·검색 로그 형식·호출 예산·LLM 캐시 스위치·날짜 계산·입력 프로필·실행 컨텍스트(Ctx)·서브그래프 State.
"""
from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import operator
import os
from pathlib import Path
import re
import threading
from typing import TYPE_CHECKING, Annotated, Any, Optional, TypedDict
from urllib.parse import urlparse

if TYPE_CHECKING:  # 타입 표기용 (실행 시 순환 import 없음)
    from .collect import DartClient, NaverNews, NpsApi, NpsSnapshots, Tavily


# ════════════════════════════════════════════════════════════
# 1. 공통 도구
# ════════════════════════════════════════════════════════════
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache"


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
def cache_on() -> bool:
    return os.environ.get("TRACTION_CACHE", "1") != "0"


def cache_refresh() -> bool:
    return os.environ.get("TRACTION_CACHE_REFRESH", "0") == "1"


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


# ── 출처 유형 분류 (수집·평가 공통: 입력 문서에 source_type이 없을 때 URL로 추정) ──
# 기업정보 집계 사이트: 회사 제출·추정 자료를 재가공 → 독립 확인 아님 (4번 정의)
AGGREGATOR_HOSTS = ("saramin.co.kr", "jobkorea.co.kr", "wanted.co.kr", "jobplanet.co.kr", "catch.co.kr",
                    "rocketpunch.com", "nicebizinfo.com", "kisline.com", "crunchbase.com")


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


RECORD_KEYS = ("revenue_records", "contract_records", "activity_records", "headcount_records", "customer_records")


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

