"""실적·성장성 분석 에이전트(4번) — 호환용 진입점: 수집(collect.py) + 평가(evaluator.py)를 한 번에 실행.

기준: 「04_실적·성장성_분석_변현준_v2」(COMMON CONTRACT 2.0) · criteria.md · schema.py

파일 구성 (2026-10-13 수집/평가 분리)
    common.py     공통 도구 (환경변수·이름·검색 로그·예산·LLM 캐시 스위치·날짜·프로필·Ctx/State)
    collect.py    수집: OpenDART · 뉴스(네이버·Tavily) · 국민연금  → Orchestrator의 01 공통 수집으로 이동 예정
    rules.py      판정 규칙 (단계·성장·추이·기대치·레드플래그·C5) — 순수 함수
    evaluator.py  평가: LLM 근거 추출 · 근거 조립 · 판정 조립 · 평가 전용 노드(traction_evaluate_node)
    agent.py      (이 파일) 기존 실행 경로 유지: 수집 + 평가 서브그래프, traction_growth_node, CLI

실행 (프로젝트 루트):
    python -m agents.traction_growth.agent 레모넥스                   # CSV 기업 행으로 최초 실행
    python -m agents.traction_growth.agent 레모넥스 --as-of 2026-09-30 --no-llm
    python -m agents.traction_growth.agent --all --as-of 2026-09-30   # CSV 전체 → 캐시 채우기 + summary.csv
    (캐시: .cache/http, .cache/llm · --refresh 새로 받기 · --no-cache 끄기)

메인 그래프 연결 (기존):
    from agents.traction_growth.agent import traction_growth_node
    # 읽기: company_profile(없으면 current_company), review_requests, clinical_analysis, risk_analysis
    # 쓰기: traction_analysis (AnalysisEnvelope dict)
Orchestrator 구조 (평가만):
    from agents.traction_growth.evaluator import traction_worker_node   # 공통 State 계약 (docs/common_state.md)

환경변수(.env): DART_API_KEY, TAVILY_API_KEY, DATA_GO_KR_API_KEY, OPENAI_API_KEY
               (선택) TRACTION_LLM_MODEL, TRACTION_LLM_TEMPERATURE, NAVER_CLIENT_ID/SECRET, NPS_API_BASE, NPS_SNAPSHOT_DIR

서브그래프 (수집은 병렬)
    START ─┬─ collect_dart ─┐
           ├─ collect_news ─┴─ extract ─┐
           └─ collect_nps ──────────────┴─ judge ─ END
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date
import json
import os
from pathlib import Path
import time
from typing import Any, Optional
import uuid

from langgraph.graph import END, START, StateGraph

from . import schema as S
from .collect import (
    DartClient,
    Http,
    NaverNews,
    NpsApi,
    NpsSnapshots,
    Tavily,
    _initial_queries,
    _review_plan,
    make_collect_dart,
    make_collect_news,
    make_collect_nps,
)
from .common import (
    CACHE_DIR,
    ROOT,
    Budget,
    Ctx,
    TGState,
    company_id_of,
    company_names,
    host_of,
    load_env,
    norm_name,
    parse_date,
    profile_from_csv,
)
from .evaluator import (
    get_llm,
    make_extract,
    make_judge,
)


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


def make_ctx(profile: dict, *, as_of: Optional[str] = None, run_id: Optional[str] = None,
             request: Optional[dict] = None, previous: Optional[dict] = None,
             related: Optional[dict] = None, llm: Any = "auto", clients: Optional[dict] = None) -> Ctx:
    """실행 컨텍스트: 외부 클라이언트·호출 예산·검색 계획(최초/보완). 수집만 할 때도 이걸로 Ctx를 만든다."""
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
    return ctx


def run_traction(profile: dict, *, as_of: Optional[str] = None, run_id: Optional[str] = None,
                 request: Optional[dict] = None, previous: Optional[dict] = None,
                 related: Optional[dict] = None, llm: Any = "auto", clients: Optional[dict] = None) -> dict:
    """한 기업 분석(수집 + 평가). request가 있으면 보완 라운드(previous 필수). 반환: AnalysisEnvelope dict."""
    ctx = make_ctx(profile, as_of=as_of, run_id=run_id, request=request, previous=previous,
                   related=related, llm=llm, clients=clients)
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
