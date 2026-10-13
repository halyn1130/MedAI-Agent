"""실적·성장성 Worker (평가 전용 단일 노드) — 공통 State 계약(docs/common_state.md 초안) 준수.

구성
    이 파일 = LLM 근거 추출 + 근거 조립(Builder) + 판정 조립(assemble) + Worker 노드·계약 변환.
    판정 규칙(단계·성장·추이·기대치·레드플래그·C5)은 rules.py, 수집은 collect.py (앞단 수집 노드로 이동 예정).
    서브그래프: START ─ extract ─ judge ─ END   (외부 검색·수집·재시도 없음, LLM은 원문 레코드 해석에만)

입력 (common_state.md 4장 Worker 입력)
    {run_id, company_id, as_of, schema_version, criteria_version, company_profile,
     dataset: {version, sources: [Source], records: [DataRecord]}, previous_result, review_request}
    - Source     : source_id, title, url, published_at, collected_at
    - DataRecord : record_id, source_id, content, locator, tags
        content 가 dict {kind, record, status?, statement?, excerpt?, event_date?} → 구조화 사실
            (kind ∈ revenue | contract | activity | headcount | customer, record = schema.py 관측값 필드)
        content 가 문자열 → 원문. LLM이 근거 후보를 뽑고, 발췌가 원문에 없으면 버린다.
        tags: 레코드 배분용. TRACTION_TAGS 중 하나가 있으면 읽음(태그가 하나도 없는 데이터셋이면 전체).
              출처 유형 태그(official·company·partner·news·…)가 있으면 독립 확인 판단에 사용.
    - 노드에서 읽는 State: review_requests["traction"], worker_results["traction"](=previous_result)

반환 (common_state.md 5장 Worker 공통 반환 결과)
    {run_id, company_id, as_of, schema_version, criteria_version, agent="traction", dataset_version,
     result_version, analysis_status, data, sources, evidence, findings, missing_items, coverage,
     review_responses, errors}
    - data = 분야별 상세(schema.TractionData: 단계·성장·추이·RF·criteria_inputs.C5·관측 records·open_questions)
    - evidence: evidence_id, record_id, source_id, excerpt, locator  (원문 연결 evidence.record_id → dataset)
    - 노드 반환은 자기 결과 변경분만: {"worker_results": {"traction": result}}

원칙
    - 호출되면 주어진 데이터로 정확히 한 번 평가. 실행 여부·재실행·횟수·중복 처리 판단 없음 (Orchestrator 몫).
    - reviewed = 제공 자료를 검토했다는 뜻 (외부 검색 완료 아님) → 근거가 없다고 '계약 없음'·0점으로 단정하지 않음.
    - 입력 계약 위반·실행 오류도 예외 대신 analysis_status=failed + errors 로 반환 (누락과 구분).

실행 (프로젝트 루트)
    # 저장된 기존 결과를 공통 데이터셋으로 되돌려 Worker만 실행 — 수집 노드 없이 테스트
    python -m agents.traction_growth.evaluator --from-envelope outputs/investment_review/traction_analysis.json --company c001
    python -m agents.traction_growth.evaluator --from-envelope outputs/investment_review/traction_analysis.json --all
    # Worker 입력 JSON 파일로 실행
    python -m agents.traction_growth.evaluator --input worker_input_c001.json
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Optional
import uuid

from langgraph.graph import END, START, StateGraph

from . import schema as S
from .common import (
    CACHE_DIR,
    RECORD_KEYS,
    Ctx,
    TGState,
    _host_in,
    _source_type,
    cache_on,
    cache_refresh,
    company_names,
    host_of,
    load_env,
    log_tool,
    norm_name,
    parse_date,
)
from .rules import (
    EXCLUDED_CONTRACT,
    c5,
    cagr,
    commercial_stage,
    expectation,
    red_flags,
    restated_years,
    revenue_by_year,
    trend,
)
from .schema import ExtractedItem, ExtractionBatch

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


UNIT_WORDS = {"조원": 10**12, "억원": 10**8, "억": 10**8, "천만원": 10**7, "백만원": 10**6,
              "만원": 10**4, "천원": 10**3, "원": 1, "krw": 1}

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
    src = {k: doc.get(k) for k in ("url", "title", "publisher", "source_type", "published_at", "source_id")}
    f = {"source": src, "statement": it.statement, "excerpt": it.excerpt if verified else None,
         "locator": doc.get("locator"), "event_date": it.event_date, "status": status, "date_uncertainty": None,
         "record_id": doc.get("record_id")}
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
        key = s.get("source_id") or s["url"]          # 공통 데이터셋 입력이면 출처 ID 기준 (URL 없을 수 있음)
        if key in self.url2src:
            return self.url2src[key]
        if s.get("source_id"):
            sid = s["source_id"]
            self.sources.append({"source_id": sid, "title": s.get("title") or sid, "url": s.get("url") or "",
                                 "publisher": s.get("publisher") or host_of(s.get("url") or "") or "unknown",
                                 "source_type": s.get("source_type") or "other", "published_at": s.get("published_at"),
                                 "checked_at": date.today().isoformat(), "original_source_id": None})
            self.url2src[key] = sid
            self.new_sources.append(sid)
            return sid
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
                              "evidence_status": f["status"], "date_uncertainty": f.get("date_uncertainty"),
                              "record_id": f.get("record_id")})
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




# ════════════════════════════════════════════════════════════
# 공통 Worker 계약 (docs/common_state.md) · 평가 노드 · CLI
# ════════════════════════════════════════════════════════════
WORKER = "traction"
FACT_KINDS = {k.removesuffix("_records") for k in RECORD_KEYS}
# 데이터셋 레코드 배분: 이 태그 중 하나가 있으면 실적·성장성 자료로 읽는다.
# 태그가 달린 레코드가 하나도 없으면 전체를 읽는다 (팀 태그 규칙 확정 전 임시 규칙).
TRACTION_TAGS = {"traction", "financial", "revenue", "disclosure", "contract", "activity", "customer",
                 "headcount", "news"} | FACT_KINDS
SOURCE_TYPE_TAGS = {"official", "company", "partner", "research", "news", "other"}
OFFICIAL_HOSTS = ("dart.fss.or.kr", "opendart.fss.or.kr", "data.go.kr", "nps.or.kr")
AREA_KINDS = {"disclosure": {"revenue"}, "revenue": {"revenue"}, "contract": {"contract"},
              "activity": {"activity"}, "customer": {"customer"}, "headcount": {"headcount"}}
TEXT_AREAS = {"contract", "activity", "customer", "revenue"}   # 원문(LLM 추출)이 근거가 될 수 있는 범위
CAUSE_TEXT = {"not_found": "제공 자료에서 확인되지 않음", "not_reviewed": "관련 자료가 데이터셋에 없음",
              "search_failed": "관련 자료 처리 실패", "input_missing": "입력 프로필에 값 없음"}


class WorkerInputError(ValueError):
    """입력 계약 위반 → 평가하지 않고 failed 결과로 반환 (code 포함)."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _source_type_of(url: Optional[str], tags: list[str], homepage_host: str) -> str:
    for t in tags or []:
        if t in SOURCE_TYPE_TAGS:
            return t                                     # 수집 노드가 출처 유형을 태그로 준 경우 우선
    if url and _host_in(host_of(url), OFFICIAL_HOSTS):
        return "official"
    return _source_type(url, homepage_host) if url else "other"


def _is_fact(content: Any) -> bool:
    return isinstance(content, dict) and content.get("kind") in FACT_KINDS and isinstance(content.get("record"), dict)


def select_records(dataset: dict) -> list[dict]:
    recs = list(dataset.get("records") or [])
    if not any(r.get("tags") for r in recs):
        return recs
    return [r for r in recs if set(r.get("tags") or []) & TRACTION_TAGS]


def dataset_to_state(dataset: dict, homepage_host: str = "") -> tuple[dict, dict]:
    """공통 데이터셋 → 평가 서브그래프 시작 State.

    - 구조화 레코드(content가 {kind, record, ...} dict) → fact (매출·계약·활동·고용·고객)
    - 원문 레코드(content가 문자열) → doc (LLM이 근거 후보를 추출, 발췌가 원문에 없으면 버림)
    - 수집 완료 여부는 계약에 없으므로 보수적으로 둔다: 원문이 있어도 news=partial
      (reviewed = 제공 자료 검토, 외부 검색 완료 아님 → '계약 없음'(RF1) 등 부재 판정은 하지 않음)
    반환: (State, 정보 {fact_records, doc_records, records_by_kind})
    """
    sources = {s["source_id"]: s for s in dataset.get("sources") or []}
    facts, docs = [], []
    by_kind: dict[str, list[str]] = {k: [] for k in FACT_KINDS}
    info = {"fact_records": [], "doc_records": [], "skipped": []}
    for r in select_records(dataset):
        s = sources.get(r.get("source_id"))
        if s is None:
            info["skipped"].append(r.get("record_id"))
            continue                                      # 계약: source_id는 데이터셋에 있어야 함
        stype = _source_type_of(s.get("url"), r.get("tags") or [], homepage_host)
        src = {"source_id": s["source_id"], "url": s.get("url") or "", "title": s.get("title"),
               "publisher": host_of(s.get("url") or "") or None, "source_type": stype,
               "published_at": s.get("published_at")}
        c = r.get("content")
        if _is_fact(c):
            facts.append({"kind": c["kind"], "record": dict(c["record"]), "source": src,
                          "status": c.get("status") or "partial", "statement": c.get("statement") or str(c["record"]),
                          "excerpt": c.get("excerpt"), "locator": r.get("locator") or c.get("locator"),
                          "event_date": c.get("event_date"), "date_uncertainty": c.get("date_uncertainty"),
                          "record_id": r["record_id"]})
            by_kind[c["kind"]].append(r["record_id"])
            info["fact_records"].append(r["record_id"])
        elif isinstance(c, str) and c.strip():
            # LLM이 근거를 원문 레코드에 정확히 되짚도록 레코드별 고유 URL을 쓴다 (출력 출처는 데이터셋 Source 그대로)
            docs.append({"url": f"record://{r['record_id']}", "title": s.get("title") or "", "text": c,
                         "source_type": stype, "published_at": s.get("published_at"),
                         "retrieved_at": s.get("collected_at"), "publisher": src["publisher"],
                         "source_id": s["source_id"], "record_id": r["record_id"], "locator": r.get("locator")})
            info["doc_records"].append(r["record_id"])
        else:
            info["skipped"].append(r.get("record_id"))
    has_official_revenue = any(f["kind"] == "revenue" and f["source"]["source_type"] == "official" for f in facts)
    status = {"disclosure": "found" if has_official_revenue else "not_reviewed",
              "news": "partial" if docs else "not_reviewed",
              "nps": "ok" if by_kind["headcount"] else "not_reviewed"}
    info["records_by_kind"] = by_kind
    return {"docs": docs, "facts": facts, "status": status, "search_log": [], "notes": []}, info


def build_eval_graph(ctx: Ctx):
    g = StateGraph(TGState)
    g.add_node("extract", make_extract(ctx))               # 원문 → fact (LLM, 발췌 대조로 환각 차단)
    g.add_node("judge", lambda state: {"envelope": assemble(ctx, state)})   # 규칙 판정 (LLM 없음)
    g.add_edge(START, "extract")
    g.add_edge("extract", "judge")
    g.add_edge("judge", END)
    return g.compile()


def _coverage(env: dict, info: dict, llm_status: Optional[str]) -> list[dict]:
    internal = {c["scope"]: c["status"] for c in env.get("coverage", [])}
    used = {e.get("record_id") for e in env.get("evidence", []) if e.get("record_id")}
    out = []
    for i, (area, kinds) in enumerate(AREA_KINDS.items(), start=1):
        rids = [rid for k in kinds for rid in info["records_by_kind"].get(k, [])]
        if area in TEXT_AREAS:
            rids += info["doc_records"]
        rids = list(dict.fromkeys(rids))
        text_unread = area in TEXT_AREAS and info["doc_records"] and llm_status in ("not_reviewed", "failed")
        if not rids:
            st, note = "not_reviewed", "관련 레코드 없음"
        elif internal.get(area) == "reviewed" and not text_unread:
            st, note = "reviewed", "제공 자료 검토 (외부 검색 완료 아님)"
        else:
            st = "partial"
            note = ("원문 레코드 LLM 미처리" if text_unread else
                    "제공 자료에 확인된 근거 없음 — 부재로 판정하지 않음" if not (set(rids) & used) else
                    "일부 근거만 확인")
        out.append({"coverage_id": f"{env['company_id']}:{WORKER}:cv{i:02d}", "area": area,
                    "record_ids": rids, "status": st, "note": note})
    return out


def _missing(env: dict) -> list[dict]:
    d = env["data"]
    c5_unknown = d["criteria_inputs"]["C5"]["score"] is None
    out = []
    for i, m in enumerate(env.get("missing_items", []), start=1):
        detail = m.get("detail") or ""
        if m["field"] == "contract_records":
            detail = "제공 자료만으로는 계약 부재를 확정할 수 없음 (외부 검색 완료 아님)"
        reason = f"{CAUSE_TEXT.get(m['cause'], m['cause'])} — {m['impact']}" + (f" ({detail})" if detail else "")
        required = c5_unknown and m["field"] in ("revenue_by_year", "contract_records")
        out.append({"missing_id": f"{env['company_id']}:{WORKER}:mi{i:02d}", "item": m["field"], "reason": reason,
                    "related_criterion_ids": ["C5"], "required_for_assessment": required})
    return out


def _review_responses(findings: list[dict], previous: Optional[dict], req: Optional[dict]) -> list[dict]:
    out = list((previous or {}).get("review_responses") or [])
    if not req:
        return out
    before = {f["finding_id"]: f["claim"] for f in (previous or {}).get("findings", [])}
    after = {f["finding_id"]: f for f in findings}
    targets = [t for t in (req.get("target_finding_ids") or list(after)) if t in after or t in before]
    changed = [t for t in targets if before.get(t) != (after.get(t) or {}).get("claim")]
    backed = bool(targets) and all((after.get(t) or {}).get("evidence_ids") for t in targets)
    for q in req.get("questions") or []:
        if changed:
            ans = "; ".join(f"{before.get(t, '(없음)')} → {(after.get(t) or {}).get('claim', '(해제)')}" for t in changed)
        else:
            ans = "같은 자료로 재검토 — 판단 유지: " + "; ".join((after.get(t) or {}).get("claim", t) for t in targets)
        if not backed:
            ans += " · 대상 판단을 뒷받침하는 근거가 제공 자료에 부족함"
        out.append({"request_id": req["request_id"], "question_id": q["question_id"], "answer": ans,
                    "finding_ids": targets, "resolved": backed})
    return out


def _result(meta: dict, *, status: str, version: int, data: Optional[dict] = None, sources=None, evidence=None,
            findings=None, missing=None, coverage=None, responses=None, errors=None) -> dict:
    res = {**meta, "agent": WORKER, "result_version": version, "analysis_status": status,
           "data": data or {}, "sources": sources or [], "evidence": evidence or [], "findings": findings or [],
           "missing_items": missing or [], "coverage": coverage or [], "review_responses": responses or [],
           "errors": errors or []}
    return S.WorkerResult.model_validate(res).model_dump(mode="json")


def evaluate_worker(worker_input: dict, llm: Any = "auto") -> dict:
    """Worker 입력(common_state.md 4장) → Worker 공통 반환 결과(5장). 예외를 던지지 않는다.

    호출되면 주어진 데이터로 한 번 평가한다. 실행 여부·횟수·중복 판단은 하지 않는다 (Orchestrator 몫).
    - previous_result 가 있으면 result_version = 이전 + 1 (재평가)
    - review_request 가 있으면 질문별 답변을 review_responses 에 추가 (결론을 바꿀 의무는 없음)
    - 입력 계약 위반·실행 오류 → analysis_status=failed + errors (누락과 구분되도록 결과를 명시적으로 반환)
    """
    w = worker_input or {}
    dataset = w.get("dataset") or {}
    previous, req = w.get("previous_result"), w.get("review_request")
    meta = {k: w.get(k) for k in ("run_id", "company_id", "as_of")}
    meta.update(schema_version=w.get("schema_version") or S.SCHEMA_VERSION,
                criteria_version=w.get("criteria_version") or S.CRITERIA_VERSION,
                dataset_version=dataset.get("version"))
    version = ((previous or {}).get("result_version") or 0) + 1
    try:
        load_env()
        if not meta["company_id"] or not meta["as_of"]:
            raise WorkerInputError("INVALID_INPUT", "company_id·as_of 는 필수")
        if req and req.get("dataset_version") and req["dataset_version"] != dataset.get("version"):
            raise WorkerInputError("DATASET_VERSION_MISMATCH",
                                   f"재검토 요청 데이터 버전 {req['dataset_version']} ≠ 입력 {dataset.get('version')}")
        S.Dataset.model_validate(dataset)
        profile = {**(w.get("company_profile") or {}), "company_id": meta["company_id"]}
        names = company_names(profile)
        if not names:
            raise WorkerInputError("INVALID_INPUT", "company_profile 에 기업명이 없음 (legal_name/name/기업명)")
        homepage = host_of(profile.get("homepage") or "")
        start, info = dataset_to_state(dataset, homepage)
        as_of_d, _ = parse_date(meta["as_of"])
        ctx = Ctx(profile=profile, as_of=as_of_d, run_id=meta["run_id"] or uuid.uuid4().hex[:12],
                  company_id=meta["company_id"], names=names,
                  invest_stage=profile.get("invest_stage") or profile.get("최근 투자단계"), homepage_host=homepage,
                  llm=(get_llm() if start["docs"] else None) if llm == "auto" else llm,
                  run_dart=False, run_news=False, run_nps=False)
        out = build_eval_graph(ctx).invoke(start)
        env, llm_status = out["envelope"], (out.get("status") or {}).get("llm")
    except WorkerInputError as e:
        return _result(meta, status="failed", version=version, errors=[{"code": e.code, "message": str(e)}],
                       responses=list((previous or {}).get("review_responses") or []))
    except Exception as e:  # noqa: BLE001 - 실행 오류도 결과로 반환
        return _result(meta, status="failed", version=version,
                       errors=[{"code": type(e).__name__, "message": str(e)[:300]}],
                       responses=list((previous or {}).get("review_responses") or []))

    errors = []
    if start["docs"] and llm_status == "not_reviewed":
        errors.append({"code": "LLM_NOT_CONFIGURED", "message": f"원문 레코드 {len(start['docs'])}건 근거 추출 생략"})
    elif llm_status in ("failed", "partial"):
        errors.append({"code": f"LLM_EXTRACTION_{llm_status.upper()}", "message": "원문 레코드 일부·전체 추출 실패"})
    coverage = _coverage(env, info, llm_status)
    status = "complete" if all(c["status"] == "reviewed" for c in coverage) and not errors else "partial"
    ds_sources = {s["source_id"]: s for s in dataset.get("sources") or []}
    used_sids = list(dict.fromkeys(sid for e in env["evidence"] for sid in e["source_ids"]))
    data = dict(env["data"])
    data["open_questions"] = list(data.get("open_questions") or []) + \
        [f"데이터셋 레코드 {rid}: source_id 없음 또는 내용 비어 있음 → 제외" for rid in info["skipped"] if rid]
    return _result(
        meta, status=status, version=version, data=data,
        sources=[ds_sources[s] for s in used_sids if s in ds_sources],
        evidence=[{"evidence_id": e["evidence_id"], "record_id": e.get("record_id"),
                   "source_id": (e.get("source_ids") or [None])[0], "excerpt": e.get("excerpt"),
                   "locator": e.get("locator")} for e in env["evidence"]],
        findings=env["findings"], missing=_missing(env), coverage=coverage,
        responses=_review_responses(env["findings"], previous, req), errors=errors)


def build_worker_input(state: dict) -> dict:
    """EvaluationState(common_state.md 2장) → Worker 입력(4장). 재평가 여부는 State에 있는 그대로 전달."""
    req = (state.get("review_requests") or {}).get(WORKER)
    return {k: state.get(k) for k in ("run_id", "company_id", "as_of", "schema_version", "criteria_version",
                                      "company_profile", "dataset")} | {
        "previous_result": (state.get("worker_results") or {}).get(WORKER),
        "review_request": req,
    }


def traction_worker_node(state: dict) -> dict:
    """메인 그래프 Worker 노드: 자기 결과 변경분만 반환 → {"worker_results": {"traction": result}}."""
    return {"worker_results": {WORKER: evaluate_worker(build_worker_input(state))}}


traction_evaluate_node = traction_worker_node   # 이전 이름 호환


# ─────────────────────────────────────────────
# 테스트용 변환: 저장된 기존 결과(AnalysisEnvelope) → 공통 데이터셋
# ─────────────────────────────────────────────
def dataset_from_envelope(env: dict, version: str = "dataset-from-envelope") -> dict:
    """기존 결과의 근거를 구조화 레코드로 되돌린다 (수집 노드 없이 실제 데이터로 시험)."""
    srcs = {s["source_id"]: s for s in env.get("sources", [])}
    ev = {e["evidence_id"]: e for e in env.get("evidence", [])}
    drop = {"evidence_ids", "contract_id", "activity_id"}
    records, n = [], 0
    for key in RECORD_KEYS:
        kind = key.removesuffix("_records")
        for r in env.get("data", {}).get(key, []):
            rec = {k: v for k, v in r.items() if k not in drop}
            if kind == "contract":
                rec["counterparty_id"] = None
            for eid in r.get("evidence_ids", []):
                e = ev.get(eid)
                if not e or not e.get("source_ids") or e["source_ids"][0] not in srcs:
                    continue
                n += 1
                s = srcs[e["source_ids"][0]]
                records.append({"record_id": f"{env['company_id']}:rec{n:03d}", "source_id": s["source_id"],
                                "locator": e.get("locator"), "tags": ["traction", kind, s.get("source_type") or "other"],
                                "content": {"kind": kind, "record": rec, "status": e["evidence_status"],
                                            "statement": e["statement"], "excerpt": e.get("excerpt"),
                                            "event_date": e.get("event_date"),
                                            "date_uncertainty": e.get("date_uncertainty")}})
    sources = [{"source_id": s["source_id"], "title": s.get("title"), "url": s.get("url") or None,
                "published_at": s.get("published_at"), "collected_at": s.get("checked_at")} for s in srcs.values()]
    return {"version": version, "sources": sources, "records": records}


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def _profiles() -> dict[str, dict]:
    from agents.investment_review.inputs import load_companies
    return {c["company_id"]: c for c in load_companies()}


def main() -> None:
    ap = argparse.ArgumentParser(description="실적·성장성 Worker 단독 실행 (공통 계약 입력, 수집 없음)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-envelope", type=Path, help="저장된 결과 {company_id: Envelope} → 공통 데이터셋으로 변환")
    src.add_argument("--input", type=Path, help="Worker 입력 JSON 파일 (common_state.md 4장 형식)")
    ap.add_argument("--company", help="company_id (예: c001)")
    ap.add_argument("--all", action="store_true", help="--from-envelope 의 모든 기업")
    ap.add_argument("--as-of", help="기준일 (기본: 저장 결과의 as_of)")
    ap.add_argument("--out", type=Path, help="결과 저장 경로 (JSON)")
    args = ap.parse_args()

    if args.input:
        jobs = [(json.loads(args.input.read_text(encoding="utf-8")), None)]
    else:
        saved = json.loads(args.from_envelope.read_text(encoding="utf-8"))
        ids = list(saved) if args.all else [args.company] if args.company else ap.error("--company 또는 --all")
        profiles = _profiles()
        jobs = []
        for cid in ids:
            p = profiles.get(cid) or {"legal_name": cid}
            jobs.append(({"run_id": "cli", "company_id": cid, "as_of": args.as_of or saved[cid].get("as_of"),
                          "schema_version": S.SCHEMA_VERSION, "criteria_version": S.CRITERIA_VERSION,
                          "company_profile": {"legal_name": p.get("company_name") or p.get("legal_name"),
                                              "aliases": [], "products": [],
                                              "invest_stage": p.get("최근 투자단계")},
                          "dataset": dataset_from_envelope(saved[cid]), "previous_result": None,
                          "review_request": None}, saved[cid]))
    out, diff = {}, 0
    for wi, old in jobs:
        res = evaluate_worker(wi, llm=None)
        out[res["company_id"]] = res
        d = res["data"] or {}
        c5 = ((d.get("criteria_inputs") or {}).get("C5") or {}).get("score")
        line = (f"{res['company_id']} 단계={d.get('commercial_stage')} 성장={d.get('growth_tier')} "
                f"C5={c5} 상태={res['analysis_status']} 근거={len(res['evidence'])} 오류={len(res['errors'])}")
        if old:
            same = (old["data"]["criteria_inputs"]["C5"]["score"] == c5 and old["data"]["commercial_stage"] == d.get("commercial_stage")
                    and old["data"]["growth_tier"] == d.get("growth_tier"))
            diff += not same
            line += "  기존과 동일" if same else f"  ※ 기존 C5={old['data']['criteria_inputs']['C5']['score']}"
        print(line)
    if args.from_envelope:
        print(f"\n기존 결과 대비 판정(단계·성장·C5) 차이: {diff} / {len(jobs)}개 기업")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out if len(out) > 1 else next(iter(out.values())),
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
