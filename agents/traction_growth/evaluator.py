"""실적·성장성 평가 노드 (수집 제거판) — Orchestrator-Workers 구조용.

왜 만들었나
    새 구조에서는 "01 공통 데이터 수집" 노드가 DART·뉴스·국민연금 자료를 모아 오고,
    실적·성장성 워커는 받은 자료로 **분석·평가만** 한다. 수집 코드(DART·뉴스·국민연금)는 collect.py 로 분리했다.
    이 파일 = LLM 근거 추출 + 근거 조립(Builder) + 판정 조립(assemble) + 평가 노드.
    판정 규칙(단계·성장·추이·기대치·레드플래그·C5)은 rules.py → 같은 입력이면 기존과 같은 판정.

서브그래프
    기존:  START ─┬─ collect_dart ─┐
                  ├─ collect_news ─┴─ extract ─┐
                  └─ collect_nps ──────────────┴─ judge ─ END
    변경:  START ─ extract ─ judge ─ END        (외부 호출 0회. LLM은 docs가 있을 때 extract에서만)

입력 계약: state["traction_inputs"]  (01 공통 데이터 수집 노드가 채운다)
    {
      "docs":   [ {url, title, publisher, source_type?, published_at?, retrieved_at?, text} ],
                # 구조화 안 된 원문(기사·공시 본문). LLM이 계약·활동·매출·고객 근거로 추출한다.
                # source_type 이 없으면 URL로 추정(official / company / news / other).
      "facts":  [ {kind, source, status, record, statement, excerpt, locator, event_date, date_uncertainty} ],
                # 이미 구조화된 사실. kind ∈ revenue | contract | activity | headcount | customer
                # (DART 매출 파싱 결과, 국민연금 가입자 수 등 — agent.py 수집 노드가 만들던 형식과 동일)
      "status": {"disclosure": found|not_found|search_failed|not_reviewed,
                 "news": ok|partial|failed|not_reviewed,
                 "nps":  ok|not_found|failed|not_reviewed},
                # 수집이 "끝까지 됐는지". 'ok/found'일 때만 "찾아봤는데 없음"으로 판정한다(RF1·none_reason).
                # 빠진 키는 not_reviewed 로 본다 = 보수적 기본값 (수집 상태를 모르면 '없음'으로 단정하지 않음)
      "search_log": [ {query, searched_at, status, entity_names, error?, source_urls?} ]   # 선택
    }

출력: state["traction_analysis"] = AnalysisEnvelope dict (기존과 동일 계약 → 심사 어댑터 수정 불필요)

보완(재분석) 요청
    Orchestrator가 "해석 불명확"으로 review_requests 를 보내면, 이전 결과(traction_analysis)와
    입력 자료로 다시 판정하고 review_responses 에 응답을 남긴다. 추가 검색은 하지 않는다
    (자료 부족 → 재수집은 01 수집 노드의 몫).

실행 (프로젝트 루트)
    # 저장된 기존 결과(Envelope)를 입력 자료로 되돌려 평가만 다시 실행 — 수집 노드 없이 바로 테스트
    python -m agents.traction_growth.evaluator --from-envelope outputs/investment_review/traction_analysis.json --company c001
    python -m agents.traction_growth.evaluator --from-envelope outputs/investment_review/traction_analysis.json --all
    # 수집 노드가 만든 입력 파일로 실행
    python -m agents.traction_growth.evaluator --inputs traction_inputs_c001.json --company c001 --as-of 2026-09-30
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
    _source_type,
    cache_on,
    cache_refresh,
    company_id_of,
    company_names,
    host_of,
    load_env,
    log_tool,
    norm_name,
    parse_date,
    profile_from_csv,
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




# ════════════════════════════════════════════════════════════
# 평가 노드 (Orchestrator 워커) · 입력 계약 · CLI
# ════════════════════════════════════════════════════════════
STATUS_KEYS = ("disclosure", "news", "nps")
EVAL_LIMITATIONS = ["평가 전용 노드: 추가 수집 없이 입력 자료(traction_inputs)로만 재판정",
                    "자료 부족 항목은 01 공통 데이터 수집 노드의 재수집 대상"]


# ─────────────────────────────────────────────
# 입력 정리
# ─────────────────────────────────────────────
def normalize_inputs(inputs: Optional[dict], homepage_host: str = "", review: bool = False) -> dict:
    """traction_inputs → 서브그래프 시작 State (docs·facts·status·search_log·notes).

    status 키가 빠졌을 때: 최초 평가면 not_reviewed(모르면 '없음'으로 단정하지 않음),
    재분석 라운드면 비워 둔다 → assemble 이 이전 결과의 상태를 유지 (이번 라운드에 재수집 안 한 범위)."""
    inputs = inputs or {}
    docs = []
    for d in inputs.get("docs") or []:
        if not d.get("url"):
            continue                                   # URL 없는 문서는 근거로 연결할 수 없음
        docs.append({**d, "source_type": d.get("source_type") or _source_type(d["url"], homepage_host)})
    facts = [f for f in inputs.get("facts") or []
             if f.get("kind") in {k.removesuffix("_records") for k in RECORD_KEYS} and f.get("source", {}).get("url")]
    status = {k: v for k, v in (inputs.get("status") or {}).items() if k in STATUS_KEYS and v}
    if not review:
        for k in STATUS_KEYS:
            status.setdefault(k, "not_reviewed")
    logs = []
    for x in inputs.get("search_log") or []:
        x = dict(x)
        x["_urls"] = list(x.pop("source_urls", None) or x.pop("_urls", None) or [])
        x.setdefault("source_ids", [])
        logs.append(x)
    return {"docs": docs, "facts": facts, "status": status, "search_log": logs,
            "notes": list(inputs.get("notes") or [])}


def inputs_from_envelope(env: dict) -> dict:
    """저장된 기존 결과(AnalysisEnvelope) → traction_inputs.

    수집 노드가 아직 없을 때 평가 노드를 실제 데이터로 시험하기 위한 변환.
    records의 각 근거(evidence)를 fact 하나로 되돌린다. ID(contract_id 등)는 평가 시 다시 매긴다.
    """
    src = {s["source_id"]: s for s in env.get("sources", [])}
    ev = {e["evidence_id"]: e for e in env.get("evidence", [])}
    data = env.get("data", {})
    drop = {"evidence_ids", "contract_id", "activity_id"}
    facts = []
    for key in RECORD_KEYS:
        kind = key.removesuffix("_records")
        for r in data.get(key, []):
            rec = {k: v for k, v in r.items() if k not in drop}
            if kind == "contract":
                rec["counterparty_id"] = None              # Builder가 거래처명으로 다시 만든다
            for eid in r.get("evidence_ids", []):
                e = ev.get(eid)
                if not e or not e.get("source_ids") or e["source_ids"][0] not in src:
                    continue
                s = src[e["source_ids"][0]]
                facts.append({"kind": kind, "record": rec, "status": e["evidence_status"],
                              "source": {k: s.get(k) for k in ("url", "title", "publisher", "source_type", "published_at")},
                              "statement": e["statement"], "excerpt": e.get("excerpt"), "locator": e.get("locator"),
                              "event_date": e.get("event_date"), "date_uncertainty": e.get("date_uncertainty")})
    cov = {c["scope"]: c for c in env.get("coverage", [])}
    note = cov.get("contract", {}).get("note", "")
    news = note.split("news_state=", 1)[1].split()[0] if "news_state=" in note else "not_reviewed"
    nps = {"reviewed": "ok", "no_relevant_evidence_found": "not_found", "search_failed": "failed"}.get(
        cov.get("headcount", {}).get("status"), "not_reviewed")
    url_of = {sid: s["url"] for sid, s in src.items()}
    logs = [{**{k: v for k, v in x.items() if k != "source_ids"},
             "source_urls": [url_of[i] for i in x.get("source_ids", []) if i in url_of]}
            for x in data.get("search_log", [])]
    return {"docs": [], "facts": facts, "search_log": logs,
            "status": {"disclosure": data.get("financial_disclosure_status") or "not_reviewed",
                       "news": news, "nps": nps}}


# ─────────────────────────────────────────────
# 서브그래프 · 실행
# ─────────────────────────────────────────────
def build_eval_graph(ctx: Ctx):
    g = StateGraph(TGState)
    g.add_node("extract", make_extract(ctx))               # docs → facts (LLM, 원문 발췌 대조로 환각 차단)
    g.add_node("judge", lambda state: {"envelope": assemble(ctx, state)})   # 규칙 판정 (LLM 없음)
    g.add_edge(START, "extract")
    g.add_edge("extract", "judge")
    g.add_edge("judge", END)
    return g.compile()


def evaluate_traction(profile: dict, inputs: Optional[dict], *, as_of: Optional[str] = None,
                      run_id: Optional[str] = None, request: Optional[dict] = None,
                      previous: Optional[dict] = None, llm: Any = "auto") -> dict:
    """한 기업 평가. request가 있으면 재분석 라운드(previous 필수). 반환: AnalysisEnvelope dict."""
    load_env()
    if request and not previous:
        raise ValueError("재분석 요청에는 이전 결과(previous)가 필요합니다")
    as_of_s = (request or {}).get("as_of") or (previous or {}).get("as_of") or as_of or date.today().isoformat()
    as_of_d, _ = parse_date(as_of_s)
    names = company_names(profile)
    if not names:
        raise ValueError("company_profile에 기업명이 없습니다 (legal_name/name/기업명)")
    homepage = host_of(profile.get("homepage") or "")
    start = normalize_inputs(inputs, homepage, review=bool(request))
    ctx = Ctx(profile=profile, as_of=as_of_d, run_id=run_id or uuid.uuid4().hex[:12],
              company_id=company_id_of(profile), names=names,
              invest_stage=profile.get("invest_stage") or profile.get("최근 투자단계"), homepage_host=homepage,
              llm=(get_llm() if start["docs"] else None) if llm == "auto" else llm,
              run_dart=False, run_news=False, run_nps=False)
    if request:
        ctx.mode, ctx.request, ctx.previous = "review", request, previous
    env = build_eval_graph(ctx).invoke(start)["envelope"]
    if request and env.get("review_responses"):
        env["review_responses"][-1]["limitations"] = list(EVAL_LIMITATIONS)
    return env


def traction_evaluate_node(state: dict) -> dict:
    """메인 그래프 워커 노드.

    읽기: company_profile, traction_inputs, as_of, run_id, traction_analysis(이전 결과), review_requests
    쓰기: traction_analysis
    - 재분석 요청이 없고 결과가 있으면 아무것도 바꾸지 않는다(재사용).
    - 재분석 요청이 있으면 이전 결과 + 입력 자료로 다시 판정한다(기업당 S.MAX_REVIEW_ROUNDS회).
    """
    profile = state.get("company_profile") or state.get("current_company") or {}
    cid = company_id_of(profile)
    inputs = state.get("traction_inputs")
    prev = state.get("traction_analysis")
    if prev and prev.get("company_id") != cid:
        prev = None
    reqs = [r for r in state.get("review_requests") or []
            if r.get("target_agent") == "traction" and r.get("company_id") == cid]
    kw = {"as_of": state.get("as_of"), "run_id": state.get("run_id")}

    if not reqs:
        return {} if prev else {"traction_analysis": evaluate_traction(profile, inputs, **kw)}
    env = prev or evaluate_traction(profile, inputs, **kw)
    done = {x["request_id"] for x in env.get("review_responses", [])}
    for r in reqs:
        if r.get("review_round", 1) > S.MAX_REVIEW_ROUNDS or r["request_id"] in done:
            continue
        env = evaluate_traction(profile, inputs, request=r, previous=env)
    return {"traction_analysis": env}


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def _profiles() -> dict[str, dict]:
    from agents.investment_review.inputs import load_companies
    return {c["company_id"]: c for c in load_companies()}


def main() -> None:
    ap = argparse.ArgumentParser(description="실적·성장성 평가 노드 단독 실행 (수집 없음)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-envelope", type=Path, help="저장된 결과 {company_id: Envelope} → 입력 자료로 변환")
    src.add_argument("--inputs", type=Path, help="traction_inputs JSON 파일 (한 기업)")
    ap.add_argument("--company", help="company_id (예: c001)")
    ap.add_argument("--all", action="store_true", help="--from-envelope 의 모든 기업")
    ap.add_argument("--as-of", help="기준일 (기본: 저장 결과의 as_of 또는 오늘)")
    ap.add_argument("--out", type=Path, help="결과 저장 경로 (JSON)")
    args = ap.parse_args()
    profiles = _profiles()

    if args.inputs:
        if not args.company:
            ap.error("--inputs 에는 --company 가 필요합니다")
        jobs = [(args.company, json.loads(args.inputs.read_text(encoding="utf-8")), args.as_of, None)]
    else:
        saved = json.loads(args.from_envelope.read_text(encoding="utf-8"))
        ids = list(saved) if args.all else [args.company] if args.company else ap.error("--company 또는 --all")
        jobs = [(cid, inputs_from_envelope(saved[cid]), args.as_of or saved[cid].get("as_of"), saved[cid]) for cid in ids]

    out, diff = {}, 0
    for cid, inputs, as_of, old in jobs:
        profile = profiles.get(cid) or profile_from_csv(cid) or {"legal_name": cid}
        env = evaluate_traction(profile, inputs, as_of=as_of, llm=None)
        out[cid] = env
        d = env["data"]
        c5_new = d["criteria_inputs"]["C5"]["score"]
        line = (f"{cid} {profile.get('company_name', '')[:10]:10} 단계={d['commercial_stage']:4} 성장={d['growth_tier']} "
                f"고용={d['headcount_trend']:7} C5={c5_new} 상태={env['analysis_status']} "
                f"근거={len(env['evidence'])}")
        if old:
            c5_old = old["data"]["criteria_inputs"]["C5"]["score"]
            same = (c5_old == c5_new and old["data"]["commercial_stage"] == d["commercial_stage"]
                    and old["data"]["growth_tier"] == d["growth_tier"])
            diff += not same
            line += "  기존과 동일" if same else f"  ※ 기존 C5={c5_old} 단계={old['data']['commercial_stage']}"
        print(line)
    if args.from_envelope:
        print(f"\n기존 결과 대비 판정 차이: {diff} / {len(jobs)}개 기업")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out if len(out) > 1 else next(iter(out.values())),
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
