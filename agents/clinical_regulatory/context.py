from __future__ import annotations

import json
import re
import threading
from typing import Any, Optional

try:
    from . import schema as S
    from .config import AGENT, DEFAULT_TARGET_COUNTRIES
    from .utils import _clean, _core_name, _dedupe, norm_date, today
except ImportError:
    import schema as S
    from config import AGENT, DEFAULT_TARGET_COUNTRIES
    from utils import _clean, _core_name, _dedupe, norm_date, today


# ══════════════════════════════════════════════
# 기업·제품 조회와 ID 발급 (수집·평가 공통)
# ══════════════════════════════════════════════
class CompanyView:
    def __init__(self, company: S.CompanyInput, as_of: str):
        self.company = company
        self.cid = company.company_id
        self.as_of, self.checked_at = as_of, today()
        self._lock = threading.Lock()
        self.counters: dict[str, int] = {}
        self.sources: dict[str, dict] = {}

    def new_id(self, kind: str) -> str:
        with self._lock:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            return f"{self.cid}:{AGENT}:{kind}{self.counters[kind]:02d}"

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
        return _dedupe(raw + cores + eng_tokens)


# ══════════════════════════════════════════════
# 기업 1곳 평가 컨텍스트 (전달받은 데이터셋을 읽기만 하고 외부 조회는 하지 않는다)
# ══════════════════════════════════════════════
class ClinicalRun(CompanyView):
    def __init__(self, company: S.CompanyInput, as_of: str, run_id: str, dataset: dict,
                 previous: Optional[dict] = None, review_request: Optional[dict] = None):
        super().__init__(company, as_of)
        self.run_id = run_id
        self.dataset_version = dataset.get("version")
        self.sources = {s["source_id"]: s for s in dataset.get("sources") or []}
        self.records: dict[str, dict] = {r["record_id"]: r for r in dataset.get("records") or []
                                         if r.get("source_id") in self.sources}
        self.previous = previous or {}
        self.review_request = review_request or {}
        self.pids: list[str] = [p.product_id for p in company.products]

        self.evidence: dict[str, dict] = {}
        self.coverage: dict[tuple, dict] = {}
        self.classify: dict[str, dict] = {}
        self.reg_records: list[dict] = []
        self.designations: list[dict] = []
        self.reimbursements: list[dict] = []
        self.studies: list[dict] = []
        self.claim_checks: list[dict] = []
        self.validation_issues: list[str] = []
        self.errors: list[dict] = []
        # score 단계 결과
        self.product_assessments: list[dict] = []
        self.red_flags: list[dict] = []
        self.findings: list[dict] = []
        self.missing_items: list[dict] = []
        self.c1: dict = {}
        self.summary = ""
        self.review_responses: list[dict] = []

    # ── 데이터셋 레코드 ─────────────────────────
    def records_for(self, area: str, pid: Optional[str] = None, country: Optional[str] = None) -> list[dict]:
        """tags로 이 범위에 배분된 레코드를 고른다.
        area 태그가 없는 레코드는 모든 영역에, product·country 태그가 없는 레코드는 모든 제품·국가에 쓴다."""
        out = []
        for r in self.records.values():
            tags = r.get("tags") or []
            areas = [t for t in tags if t.startswith("area:")]
            if areas:
                if f"area:{area}" not in areas:
                    continue
            elif tags and AGENT not in tags:
                continue  # 다른 Worker에 배분된 자료
            products = [t for t in tags if t.startswith("product:")]
            if pid and products and f"product:{pid}" not in products:
                continue
            countries = [t for t in tags if t.startswith("country:")]
            if country and countries and f"country:{country}" not in countries:
                continue
            out.append(r)
        return out

    def record_view(self, r: dict) -> dict:
        """LLM 입력용: 레코드 내용과 출처 정보를 함께 보여준다"""
        src = self.sources[r["source_id"]]
        return {"record_id": r["record_id"], "title": src.get("title"), "url": src.get("url"),
                "published_at": src.get("published_at"), "locator": r.get("locator"), "content": r.get("content")}

    def record_title(self, rid: str) -> str:
        return self.sources.get(self.records.get(rid, {}).get("source_id"), {}).get("title") or ""

    def record_text(self, rid: str) -> str:
        """출처 제목 + 레코드 원문 (LLM이 쓴 문장이 아닌 원자료)"""
        content = self.records.get(rid, {}).get("content")
        if content is not None and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return f"{self.record_title(rid)} {content or ''}"

    def is_mfds(self, rid: str) -> bool:
        """식약처 공식 기록인지 (업체명으로 걸러 조회한 기관 DB 자료)"""
        r = self.records.get(rid, {})
        src = self.sources.get(r.get("source_id"), {})
        url = src.get("url") or ""
        return (any(t.startswith("tool:mfds") for t in r.get("tags") or [])
                or "apis.data.go.kr/1471000" in url or "mfds.go.kr" in url
                or src.get("publisher") == "식품의약품안전처")

    # ── Evidence ───────────────────────────────
    def add_evidence(self, product_id: Optional[str], statement: str, record_ids: list[str], status: str,
                     excerpt: Optional[str] = None, event_date: Optional[str] = None,
                     geography: Optional[str] = None) -> list[str]:
        """근거가 된 레코드마다 Evidence를 하나씩 만든다. 데이터셋에 없는 record_id는 버린다."""
        if not statement:
            return []
        eids = []
        for rid in _dedupe([r for r in record_ids if r in self.records]):
            same = next((e["evidence_id"] for e in self.evidence.values() if e["record_id"] == rid
                         and e["product_id"] == product_id and e["statement"] == statement), None)
            if same:  # 같은 사실은 기존 ID 재사용
                eids.append(same)
                continue
            eid = self.new_id("ev")
            rec = self.records[rid]
            self.evidence[eid] = S.Evidence(
                evidence_id=eid, record_id=rid, source_id=rec["source_id"],
                excerpt=(excerpt or None) and excerpt[:500], locator=rec.get("locator"),
                product_id=product_id, statement=statement, event_date=norm_date(event_date),
                geography=geography, evidence_status=status,
            ).model_dump()
            eids.append(eid)
        return eids

    def evidence_records(self, evidence_ids: list[str]) -> list[str]:
        return _dedupe([self.evidence[e]["record_id"] for e in evidence_ids if e in self.evidence])

    # ── Coverage ───────────────────────────────
    def set_coverage(self, area: str, pid: Optional[str], country: Optional[str], status: str,
                     records: Optional[list[dict]] = None, note: Optional[str] = None) -> None:
        self.coverage[(area, pid, country)] = S.Coverage(
            coverage_id=self.new_id("cv"), area=area, record_ids=[r["record_id"] for r in records or []],
            status=status, note=note, product_id=pid, country=country,
        ).model_dump()

    # ── 오류 · 재검토 ───────────────────────────
    def add_error(self, code: str, error: Exception) -> None:
        self.errors.append(S.WorkerError(code=code, message=f"{type(error).__name__}: {error}"[:300]).model_dump())

    def review_payload(self) -> dict:
        """재검토 요청이 있으면 LLM 입력에 덧붙인다 (같은 자료를 지적 사항에 비추어 다시 판단)"""
        req = self.review_request
        if not req:
            return {}
        targets = set(req.get("target_finding_ids") or [])
        return {"재검토 요청": {
            "reason": req.get("reason"), "questions": req.get("questions") or [],
            "이전 결과의 대상 Finding": [f for f in self.previous.get("findings") or [] if f.get("finding_id") in targets],
        }}


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

