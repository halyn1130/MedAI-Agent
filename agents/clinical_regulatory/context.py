from __future__ import annotations

import re
import threading
from typing import Any, Callable, Optional, TypedDict

try:
    from . import schema as S
    from .config import AGENT, DEFAULT_TARGET_COUNTRIES
    from .utils import _clean, _core_name, _dedupe, compare_to_as_of, norm_date, today
except ImportError:
    import schema as S
    from config import AGENT, DEFAULT_TARGET_COUNTRIES
    from utils import _clean, _core_name, _dedupe, compare_to_as_of, norm_date, today


# ══════════════════════════════════════════════
# 호출 예산
# ══════════════════════════════════════════════
class Budget:
    """보완 요청의 search_budget.max_calls. 최초 실행은 제한 없음(None)."""

    def __init__(self, max_calls: Optional[int]):
        self.max_calls, self.used = max_calls, 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.max_calls is not None and self.used >= self.max_calls:
                return False
            self.used += 1
            return True


# ══════════════════════════════════════════════
# 기업 1곳 실행 컨텍스트
# ══════════════════════════════════════════════
_ID_RE = re.compile(r":(src|ev|fd|reg|des|rmb|st|rf|mi|cv|rr)(\d+)$")


class ClinicalRun:
    def __init__(self, company: S.CompanyInput, as_of: str, run_id: str,
                 previous: Optional[dict], requests_: list[S.ReviewRequest], over_limit: list[S.ReviewRequest]):
        self.company = company
        self.cid = company.company_id
        self.as_of, self.run_id, self.checked_at = as_of, run_id, today()
        self.previous = previous or {}
        self.requests = requests_
        self.over_limit = over_limit
        self.mode = "review" if (requests_ or over_limit) else "initial"
        self.budget = Budget(sum(r.search_budget.max_calls for r in requests_) if requests_ else None)
        self._lock = threading.Lock()

        prev, pdata = self.previous, self.previous.get("data") or {}
        self.prev_data = pdata
        self.prev_keys: dict[str, str] = dict(pdata.get("id_keys", {}))   # 보완 시 Finding·Flag ID 유지
        self.id_keys: dict[str, str] = {}
        self.sources: dict[str, dict] = {s["source_id"]: s for s in prev.get("sources", [])}
        self.url_to_sid = {s["url"]: s["source_id"] for s in self.sources.values()}
        self.evidence: dict[str, dict] = {e["evidence_id"]: e for e in prev.get("evidence", [])}
        self.prev_findings: list[dict] = prev.get("findings", [])
        self.prev_missing: list[dict] = prev.get("missing_items", [])
        self.coverage: dict[tuple, dict] = {(c["area"], c.get("product_id"), c.get("country")): c
                                            for c in prev.get("coverage", [])}
        self.classify: dict[str, dict] = {
            pa["product_id"]: {"product_type": pa["product_type"],
                               "regulatory_applicability": pa["regulatory_applicability"],
                               "has_efficacy_claim": None, "rationale": "이전 결과 유지"}
            for pa in pdata.get("product_assessments", [])
        }
        self.reg_records: list[dict] = list(pdata.get("regulatory_records", []))
        self.designations: list[dict] = list(pdata.get("designation_records", []))
        self.reimbursements: list[dict] = list(pdata.get("reimbursement_records", []))
        self.studies: list[dict] = list(pdata.get("clinical_studies", []))
        self.claim_checks: list[dict] = list(pdata.get("claim_checks", []))

        self.counters = self._init_counters()
        self.steps: list[str] = []
        self.target_pids: list[str] = []
        self.new_source_ids: list[str] = []
        self.search_attempts, self.search_ok = 0, 0
        self.validation_issues: list[str] = []
        self.memo: dict[tuple, dict] = {}
        self.source_text: dict[str, str] = {}   # source_id → 검색 결과 본문·초록 (LLM이 쓴 문장이 아닌 원문)
        # score 단계 결과
        self.product_assessments: list[dict] = []
        self.red_flags: list[dict] = []
        self.findings: list[dict] = []
        self.missing_items: list[dict] = []
        self.c1: dict = {}
        self.summary = ""
        self.review_responses: list[dict] = []
        self.raw_profile: dict = {}
        self.collected: dict = dict(pdata.get("collected_profile", {}))
        self.self_collected = bool(self.collected)

    # ── ID ─────────────────────────────────────
    def _init_counters(self) -> dict[str, int]:
        counters: dict[str, int] = {}
        ids = [s for s in self.sources] + [e for e in self.evidence]
        ids += [f["finding_id"] for f in self.prev_findings] + [m["item_id"] for m in self.prev_missing]
        ids += [c["coverage_id"] for c in self.coverage.values()]
        for key in ("regulatory_records", "designation_records", "reimbursement_records"):
            ids += [r["record_id"] for r in self.prev_data.get(key, [])]
        ids += [s["study_id"] for s in self.prev_data.get("clinical_studies", [])]
        ids += [f["flag_id"] for f in self.prev_data.get("red_flags", [])]
        ids += [r["response_id"] for r in self.previous.get("review_responses", [])]
        for i in ids:
            m = _ID_RE.search(i or "")
            if m:
                counters[m.group(1)] = max(counters.get(m.group(1), 0), int(m.group(2)))
        return counters

    def new_id(self, kind: str) -> str:
        with self._lock:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            return f"{self.cid}:{AGENT}:{kind}{self.counters[kind]:02d}"

    @staticmethod
    def reuse_id(prev_items: list[dict], id_field: str, key_fn: Callable[[dict], Any], key: Any) -> Optional[str]:
        for it in prev_items:
            if key_fn(it) == key:
                return it[id_field]
        return None

    # ── Source · Evidence ──────────────────────
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
                self.sources[sid]["checked_at"] = self.checked_at
                if source_type == "company":  # 홈페이지로 확인되면 유형 갱신
                    self.sources[sid]["source_type"] = "company"
                return sid
        sid = self.new_id("src")
        src = S.Source(source_id=sid, title=(title or url)[:300], url=url, publisher=publisher,
                       source_type=source_type, published_at=pub, checked_at=self.checked_at).model_dump()
        with self._lock:
            self.sources[sid] = src
            self.url_to_sid[url] = sid
            self.new_source_ids.append(sid)
        return sid

    def add_evidence(self, product_id: Optional[str], statement: str, source_ids: list[str], status: str,
                     excerpt: Optional[str] = None, event_date: Optional[str] = None,
                     geography: Optional[str] = None, locator: Optional[str] = None) -> Optional[str]:
        valid = [s for s in source_ids if s in self.sources]
        if not valid or not statement:
            return None
        for e in self.evidence.values():  # 같은 사실은 기존 ID 재사용
            if e["product_id"] == product_id and e["statement"] == statement and set(e["source_ids"]) == set(valid):
                return e["evidence_id"]
        eid = self.new_id("ev")
        self.evidence[eid] = S.Evidence(
            evidence_id=eid, company_id=self.cid, product_id=product_id, statement=statement,
            source_ids=valid, locator=locator, excerpt=(excerpt or None) and excerpt[:500],
            event_date=norm_date(event_date), geography=geography, evidence_status=status,
        ).model_dump()
        return eid

    # ── Coverage ───────────────────────────────
    def set_coverage(self, area: str, pid: Optional[str], country: Optional[str], status: str,
                     names=None, queries=None, sources=None, note: Optional[str] = None) -> None:
        key = (area, pid, country)
        old = self.coverage.get(key)
        cid = old["coverage_id"] if old else self.new_id("cv")
        self.coverage[key] = S.Coverage(
            coverage_id=cid, area=area, product_id=pid, country=country, status=status,
            search_scope=S.SearchScope(names=_dedupe(names or []), queries=_dedupe(queries or []),
                                       sources=_dedupe(sources or [])),
            checked_at=self.checked_at, note=note,
        ).model_dump()

    def tried_names(self) -> set[str]:
        out: set[str] = set()
        for c in self.coverage.values():
            out |= set(c.get("search_scope", {}).get("names", []))
        return out

    # ── 편의 ───────────────────────────────────
    def product(self, pid: str) -> S.ProductInput:
        return next(p for p in self.company.products if p.product_id == pid)

    @staticmethod
    def _extract_brand_tokens(name: str) -> list[str]:
        stop_words = {
            "and", "the", "for", "with", "system", "solution", "software", "cardio", "plus",
            "medical", "device", "model", "series", "care", "health", "smart", "ai", "a.i.",
            "prostate", "patch", "eye", "app", "web", "data", "cloud", "core", "platform",
            "기반", "진단", "보조", "소프트웨어", "시스템", "솔루션", "프로그램", "의료기기",
            "의료영상", "분석", "장치", "기기", "키트", "검사", "치료", "서비스", "플랫폼", "등",
            "진단보조", "진단시스템", "보조소프트웨어", "인공지능", "모니터링", "패치", "카메라", "장갑", "지원",
        }
        tokens = []
        for raw_part in re.split(r"[\s/(),\[\]]+", name):
            part = re.sub(r"-(plus|pro|max|mini|ex|lite|2000|1000|[0-9]+)$", "", raw_part, flags=re.I).strip("-._ ")
            if len(part) >= 2 and part.lower() not in stop_words and not part.isdigit():
                tokens.append(part)
        return tokens

    def product_names(self, pid: str) -> list[str]:
        p = self.product(pid)
        raw = [p.name, p.model, *getattr(p, "aliases", [])]
        if pid == "p001":
            for op in self.company.products:
                if op.product_id != "p001":
                    raw += [op.name, op.model, *getattr(op, "aliases", [])]
        extracted = []
        for r in raw:
            if not r:
                continue
            extracted.append(r)
            for part in re.split(r"[\(\)\[\]/]", r):
                clean = part.strip()
                if len(clean) >= 2:
                    extracted.append(clean)
            for bt in self._extract_brand_tokens(r):
                if len(bt) >= 2:
                    extracted.append(bt)
        return _dedupe(extracted)

    def company_names(self) -> list[str]:
        c = self.company
        raw = _dedupe([c.legal_name, c.display_name, c.english_name, *c.aliases])
        cores = _dedupe([_core_name(n) for n in raw if n])
        eng_tokens = []
        if c.english_name:
            words = [w.strip() for w in re.split(r"[\s,]+", c.english_name) if len(w.strip()) >= 3]
            common_words = {"inc", "ltd", "corp", "corporation", "company", "co", "the", "and"}
            eng_tokens = [w for w in words if w.lower() not in common_words]
        if self.mode == "initial":
            return _dedupe(raw + cores + eng_tokens)
        # 보완: 이전에 조회하지 않은 표기를 우선
        variants = []
        for core in cores:
            variants += [core, f"주식회사 {core}", f"(주){core}", f"{core}(주)", f"{core} 주식회사"]
        variants += [c.english_name, *eng_tokens]
        tried = self.tried_names()
        fresh = [v for v in _dedupe(variants) if v not in tried]
        return fresh or _dedupe(variants)


class ClinicalAgentState(TypedDict, total=False):
    ctx: ClinicalRun
    envelope: dict


# ══════════════════════════════════════════════
# 입력 정규화
# ══════════════════════════════════════════════
def _get(d: dict, *keys: str) -> Any:
    for k in keys:
        v = _clean(d.get(k))
        if v not in (None, "", []):
            return v
    return None


def normalize_profile(company_id: str, raw: dict) -> S.CompanyInput:
    """v2 company_profile 또는 CSV 행을 CompanyInput으로 변환"""
    display = _get(raw, "display_name", "name", "기업명") or _get(raw, "legal_name", "법인 정식명") or company_id
    aliases = raw.get("aliases") or []
    products_raw = raw.get("products") or []
    if not products_raw:
        main = _get(raw, "주요 제품/서비스", "description")
        products_raw = [{"name": main}] if main else []
    company_claims = raw.get("claims") or []
    products: list[S.ProductInput] = []
    for i, p in enumerate(products_raw, 1):
        pid = p.get("product_id") or f"p{i:03d}"
        raw_claims = list(p.get("claims") or []) + (company_claims if i == 1 else [])
        claims = []
        for j, c in enumerate(raw_claims, 1):
            c = {"text": c} if isinstance(c, str) else c
            text = _get(c, "text", "claim", "original_claim", "원문")
            if text:
                claims.append(S.ClaimInput(
                    claim_id=c.get("claim_id") or f"{company_id}:{pid}:cl{j:02d}", text=text,
                    claimed_at=norm_date(_get(c, "claimed_at", "date")), url=_get(c, "url", "source_url"),
                    locator=c.get("locator"), product_id=pid, country=c.get("country")))
        products.append(S.ProductInput(
            product_id=pid, name=_get(p, "name", "product_name", "제품명"), model=_get(p, "model", "모델명"),
            version=_clean(p.get("version")), manufacturer=_clean(p.get("manufacturer")),
            intended_use=_get(p, "intended_use", "용도"), target_disease=_clean(p.get("target_disease")),
            users=_clean(p.get("users")), environment=_clean(p.get("environment")), modality=_clean(p.get("modality")),
            target_countries=p.get("target_countries") or list(DEFAULT_TARGET_COUNTRIES),
            scope_weight=float(p.get("scope_weight") or 1.0), claims=claims))
    return S.CompanyInput(
        company_id=company_id, display_name=display, legal_name=_get(raw, "legal_name", "법인 정식명", "법인명"),
        aliases=aliases, english_name=_get(raw, "english_name", "영문명"), homepage=_get(raw, "homepage", "홈페이지"),
        description=_get(raw, "description", "사업 설명", "주요 제품/서비스"), products=products)

