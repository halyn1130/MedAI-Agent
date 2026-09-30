"""검증 이슈 → Agent별 보완 요청. 각 Agent가 받는 형식 그대로 만든다. 기업당 1라운드.

형식 (팀원 코드 기준)
- clinical: agents/clinical_regulatory/schema.py ReviewRequest (blocker: bool, previous_result_version 필수)
- market  : agents/market/schema.py ReviewRequest (extra 필드 금지, blocker: str, dimensions로 재실행 영역 지정)
- traction: agents/traction_growth/schema.py ReviewRequest (blocker: str, previous_result_version 필수)
- risk    : {company_id, attempt=1, reason, questions: [str]}  (agents/risk/agent.py load_dataset)

State에 넣는 방식은 다르다: clinical·market·traction은 review_requests list의 원소,
risk는 review_requests["risk"]. 이 변환은 nodes.py가 한다.
"""
from __future__ import annotations

from typing import Iterable

from .contract import AgentResult, IssueKind, ValidationIssue
from .policy import CRITERION_OWNER

MAX_REVIEW_ROUNDS = 1
SEARCH_BUDGET = 5                      # 보완 요청당 추가 호출 (설계서 25쪽)
REASON_PRIORITY = (IssueKind.CONFLICT, IssueKind.INTERPRETATION_ERROR,
                   IssueKind.STALE_INFORMATION, IssueKind.MISSING_EVIDENCE)
MARKET_DIMENSIONS = {"C2": ["size_growth"], "C3": ["demand", "commercialization"], "C4": ["monetization"]}

QUESTION = {
    "score_unknown": "{criterion} 점수를 산출할 근거를 추가로 확인할 수 있습니까? 불가하면 사유와 조사 범위를 남겨 주세요.",
    "unknown_evidence": "{criterion} 점수가 참조한 근거 ID({ids})가 결과에 없습니다. 근거를 연결하거나 점수를 다시 판단해 주세요.",
    "no_evidence": "{criterion} 점수에 근거 ID가 없습니다. 근거를 연결하거나 점수를 다시 판단해 주세요.",
    "as_of_mismatch": "기준일이 심사 기준일과 다릅니다. 심사 기준일로 다시 분석해 주세요. ({detail})",
    "unconfirmed_checks": "{criterion}의 미확인 체크({ids})를 확인할 근거가 있습니까? 확인되면 yes/no로 갱신해 주세요.",
    "future_source": "기준일 이후 발행 출처({ids})가 포함되었습니다. 점수 근거에서 제외했는지 확인해 주세요.",
}


def _code(issue: ValidationIssue) -> str:
    return issue.issue_id.split(":")[2]


def _question(issue: ValidationIssue) -> str:
    template = QUESTION.get(_code(issue), "{detail}")
    return template.format(criterion=issue.criterion_id or "", ids=", ".join(issue.related_ids[:5]), detail=issue.detail)


def _common(company_id: str, agent: str, issues: list[ValidationIssue]) -> dict:
    kinds = {i.kind for i in issues}
    reason = next(k for k in REASON_PRIORITY if k in kinds).value
    criteria = sorted({i.criterion_id for i in issues if i.criterion_id})
    owned = [c for c, owner in CRITERION_OWNER.items() if owner == agent]
    blocking = [i for i in issues if i.blocking]
    return dict(
        request_id=f"{company_id}:{agent}:rr1",
        company_id=company_id,
        target_agent=agent,
        criteria=criteria or owned,
        reason=reason,
        blocker="; ".join(i.detail for i in blocking or issues),
        blocking=bool(blocking),
        questions=[{"question_id": f"q{n:02d}", "text": _question(i)} for n, i in enumerate(issues, start=1)],
        related_evidence_ids=sorted({e for i in issues for e in i.related_ids}),
        completion_conditions=[f"{c} 점수 산출 또는 산출 불가 사유·조사 범위 기록" for c in criteria or owned],
    )


def _clinical(c: dict, result: AgentResult, as_of: str) -> dict:
    return dict(request_id=c["request_id"], company_id=c["company_id"], target_agent="clinical",
                criterion_id=c["criteria"][0], reason=c["reason"], blocker=c["blocking"],
                questions=c["questions"], related_evidence_ids=c["related_evidence_ids"],
                previous_result_version=result.result_version or 1, as_of=as_of, review_round=1,
                search_budget={"max_calls": SEARCH_BUDGET}, completion_conditions=c["completion_conditions"])


def _traction(c: dict, result: AgentResult, as_of: str) -> dict:
    return dict(request_id=c["request_id"], company_id=c["company_id"], target_agent="traction",
                criterion_id=c["criteria"][0], reason=c["reason"], blocker=c["blocker"],
                questions=c["questions"], related_evidence_ids=c["related_evidence_ids"],
                previous_result_version=result.result_version or 1, as_of=as_of, review_round=1,
                search_budget={"max_calls": SEARCH_BUDGET}, completion_conditions=c["completion_conditions"])


def _market(c: dict, result: AgentResult, as_of: str) -> dict:
    dims = sorted({d for crit in c["criteria"] for d in MARKET_DIMENSIONS.get(crit, [])})
    return dict(request_id=c["request_id"], company_id=c["company_id"], target_agent="market",
                criterion_id=c["criteria"][0], reason=c["reason"], blocker=c["blocker"],
                questions=c["questions"], related_evidence_ids=c["related_evidence_ids"], review_round=1,
                completion_conditions=c["completion_conditions"], dimensions=dims)


def _risk(c: dict, result: AgentResult, as_of: str) -> dict:
    return dict(request_id=c["request_id"], company_id=c["company_id"], attempt=1, reason=c["reason"],
                questions=[q["text"] for q in c["questions"]])


FORMATTERS = {"clinical": _clinical, "market": _market, "traction": _traction, "risk": _risk}


def build_review_requests(company_id: str, as_of: str, issues: Iterable[ValidationIssue],
                          results: Iterable[AgentResult], review_round: int = 0) -> dict[str, dict]:
    """{agent: 그 Agent 형식의 요청}. 이미 1라운드를 했거나 보완할 이슈가 없으면 빈 dict."""
    if review_round >= MAX_REVIEW_ROUNDS:
        return {}
    by_agent = {r.agent: r for r in results}
    grouped: dict[str, list[ValidationIssue]] = {}
    for i in issues:
        result = by_agent.get(i.target_agent)
        # 보완 불가 이슈, 결과 자체가 없는 Agent(not_run), 분석 실패는 보완 요청 대상이 아니다
        if not i.reviewable or result is None or result.analysis_status in ("not_run", "failed"):
            continue
        grouped.setdefault(i.target_agent, []).append(i)
    return {agent: FORMATTERS[agent](_common(company_id, agent, grouped[agent]), by_agent[agent], as_of)
            for agent in sorted(grouped) if agent in FORMATTERS}
