"""보고서 노드 단독 실행 그래프 + 임시 Orchestrator.

팀 Orchestrator가 완성되면 이 파일의 temp_orchestrator 대신 그쪽 분기에 노드 3개를 연결한다.
노드(report/nodes.py)는 바꿀 필요가 없다.

    START → aggregate_results → write_report → review_quality → temp_orchestrator ─(write_report)→ write_report
                                                                                  └─(finish)──────→ END

임시 Orchestrator 규칙 (common_state.md 1번 흐름 중 보고서 부분만)
- quality_review.status == pass                             → finish
- kind=report 이슈가 있고 report_revision < retry_limits     → write_report (재작성, 횟수 +1)
- 그 외(재작성 상한 도달, 데이터 부족·해석·실행 문제만 남음) → finish, termination_reason 기록
  데이터 수집·Worker 재검토 경로는 팀 Orchestrator가 맡으므로 여기서는 종료하고 이슈를 남긴다.
"""
from __future__ import annotations

from typing import Optional

from langgraph.graph import END, START, StateGraph

from .nodes import ReportConfig, ReportStageState, _log, aggregate_node, quality_node, write_node

REPORT_REVISION_LIMIT = 2      # 재작성 횟수 상한 (최초 작성 제외) → 최대 3회 작성


def temp_orchestrator(state: dict) -> dict:
    orch = dict(state.get("orchestration") or {})
    counts = dict(orch.get("retry_counts") or {})
    limits = {"report_revision": REPORT_REVISION_LIMIT, **(orch.get("retry_limits") or {})}
    review = state["quality_review"]
    used = counts.get("report_revision", 0)
    has_report = any(i["kind"] == "report" for i in review["issues"])
    if review["status"] == "pass":
        action, reason, status = "finish", None, "complete"
    elif has_report and used < limits["report_revision"]:
        action, reason, status = "write_report", None, "running"
        counts["report_revision"] = used + 1
    else:
        action, status = "finish", "failed" if review["status"] == "failed" else "partial"
        reason = "report_revision_limit" if has_report else "unresolved_non_report_issues"
    _log(state, "temp_orchestrator", action,
         reason or f"quality_review={review['status']} · 재작성 {counts.get('report_revision', 0)}/{limits['report_revision']}")
    return {"orchestration": {**orch, "status": status, "next_action": action, "retry_counts": counts,
                              "retry_limits": limits, "termination_reason": reason}}


def route(state: dict) -> str:
    return "write_report" if state["orchestration"]["next_action"] == "write_report" else END


def build_report_graph(cfg: Optional[ReportConfig] = None):
    cfg = (cfg or ReportConfig()).resolved()
    g = StateGraph(ReportStageState)
    g.add_node("aggregate_results", aggregate_node(cfg))
    g.add_node("write_report", write_node(cfg))
    g.add_node("review_quality", quality_node(cfg))
    g.add_node("temp_orchestrator", temp_orchestrator)
    g.add_edge(START, "aggregate_results")
    g.add_edge("aggregate_results", "write_report")
    g.add_edge("write_report", "review_quality")
    g.add_edge("review_quality", "temp_orchestrator")
    g.add_conditional_edges("temp_orchestrator", route, ["write_report", END])
    return g.compile()
