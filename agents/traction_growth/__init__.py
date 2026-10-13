"""실적·성장성 분석 에이전트(4번). 메인 그래프에서는 traction_growth_node 를 노드로 등록한다.

    from agents.traction_growth import traction_growth_node

Orchestrator 구조(공통 State 계약, docs/common_state.md)에서는 평가 전용 Worker 노드를 쓴다:
    from agents.traction_growth import traction_worker_node   # 읽기: dataset 등 → {"worker_results": {"traction": ...}}
"""
__all__ = ["run_traction", "traction_growth_node", "evaluate_worker", "traction_worker_node", "traction_evaluate_node"]
_MODULE = {"run_traction": "agent", "traction_growth_node": "agent", "evaluate_worker": "evaluator",
           "traction_worker_node": "evaluator", "traction_evaluate_node": "evaluator"}


def __getattr__(name):  # 지연 import: `python -m agents.traction_growth.agent` 실행 시 경고 방지
    if name in __all__:
        from importlib import import_module
        return getattr(import_module(f".{_MODULE[name]}", __name__), name)
    raise AttributeError(name)
