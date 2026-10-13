"""실적·성장성 분석 에이전트(4번). 메인 그래프에서는 traction_growth_node 를 노드로 등록한다.

    from agents.traction_growth import traction_growth_node

Orchestrator 구조(수집 분리)에서는 평가 전용 노드를 쓴다:
    from agents.traction_growth import traction_evaluate_node   # 입력: state["traction_inputs"]
"""
__all__ = ["run_traction", "traction_growth_node", "evaluate_traction", "traction_evaluate_node"]
_MODULE = {"run_traction": "agent", "traction_growth_node": "agent",
           "evaluate_traction": "evaluator", "traction_evaluate_node": "evaluator"}


def __getattr__(name):  # 지연 import: `python -m agents.traction_growth.agent` 실행 시 경고 방지
    if name in __all__:
        from importlib import import_module
        return getattr(import_module(f".{_MODULE[name]}", __name__), name)
    raise AttributeError(name)
