"""실적·성장성 분석 에이전트(4번). 메인 그래프에서는 traction_growth_node 를 노드로 등록한다.

    from agents.traction_growth import traction_growth_node
"""
__all__ = ["run_traction", "traction_growth_node"]


def __getattr__(name):  # 지연 import: `python -m agents.traction_growth.agent` 실행 시 경고 방지
    if name in __all__:
        from . import agent
        return getattr(agent, name)
    raise AttributeError(name)
