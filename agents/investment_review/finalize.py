"""기업 1개의 Worker 결과 → InvestmentReview (어댑터 → 검증 → 판정). 기업별 그래프 finalize와 보고서 취합 노드가 함께 쓴다.

worker_results는 {worker: 결과}. 결과는 각 Agent의 기존 형식이나 공통 Worker 형식(common_state.md 5번) 모두 된다.
risk 결과의 출처는 결과 안 sources(공통 형식) 또는 references 인자(기존 형식)로 받는다.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .adapters import adapt_clinical, adapt_market, adapt_risk, adapt_traction, not_run
from .contract import AgentResult, InvestmentReview, ReportDraft
from .judge import judge
from .policy import DEFAULT_POLICY, Policy
from .validate import validate

WORKERS = ("clinical", "market", "traction", "risk")
ADAPTERS = {"clinical": adapt_clinical, "market": adapt_market, "traction": adapt_traction}


def adapt_worker_results(company_id: str, worker_results: dict, references: Optional[list] = None) -> list[AgentResult]:
    out = [ADAPTERS[w](worker_results[w]) if worker_results.get(w) else not_run(w, company_id)
           for w in ("clinical", "market", "traction")]
    risk = worker_results.get("risk")
    out.append(adapt_risk(risk, references) if risk else not_run("risk", company_id))
    return out


def finalize_review(company_id: str, as_of: str, results: list[AgentResult], worker_results: dict,
                    request_ids: Iterable[str] = (), policy: Policy = DEFAULT_POLICY) -> InvestmentReview:
    issues = validate(company_id, as_of, results, policy)
    review = judge(company_id, as_of, [c for r in results for c in r.criteria], [g for r in results for g in r.gates],
                   issues, policy)
    return review.model_copy(update={
        "review_request_ids": list(request_ids),
        "review_response_ids": [x.get("response_id") or x.get("request_id") for w in WORKERS
                                for x in (worker_results.get(w) or {}).get("review_responses", [])],
        "remaining_unknowns": review.remaining_unknowns + [u for r in results for u in r.unknowns],
        "report": ReportDraft(concerns=[f"[{r.agent}] {c}" for r in results for c in r.concerns],
                              due_diligence_questions=[q for r in results for q in r.due_diligence_questions]),
        "source_ids": sorted({s for r in results for s in r.source_ids}),
    })
