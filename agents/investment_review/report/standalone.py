"""보고서 단독 실행 그래프: 저장된 실행 결과로 보고서만 다시 만든다 (Worker·Orchestrator 미호출).

    START → aggregate_results → write_report → review_quality ─(report 이슈 & 재작성 < 상한)→ write_report
                                                              └──────────────────────────────→ END

Worker를 다시 부를 수 없으므로 interpretation 이슈(Worker 결과 이상)는 재검토하지 않고 기록만 한다
(termination_reason=rereview_unavailable). Orchestrator와 연결한 전체 그래프는 investment_review/pipeline.py.
"""
from __future__ import annotations

from typing import Optional

from langgraph.graph import START, StateGraph

from .nodes import ReportConfig, ReportStageState, add_report_nodes


def build_report_graph(cfg: Optional[ReportConfig] = None):
    cfg = (cfg or ReportConfig()).resolved()
    cfg.rereview = False
    g = StateGraph(ReportStageState)
    add_report_nodes(g, cfg)
    g.add_edge(START, "aggregate_results")
    return g.compile()
