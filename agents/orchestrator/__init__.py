"""Orchestrator-Workers: 수집된 데이터셋을 보고 자료가 있는 Worker(임상·시장·실적·리스크)만 골라 실행한다.

    from agents.orchestrator import build_company_graph, build_graph, plan_workers
"""
from .routing import WORKERS, merge_datasets, plan_workers, slice_dataset

__all__ = ["WORKERS", "plan_workers", "slice_dataset", "merge_datasets"]   # + build_company_graph·build_graph (지연 로딩)


def __getattr__(name):  # langgraph·심사 모듈은 그래프가 필요할 때만 불러온다
    if name in ("build_company_graph", "build_graph"):
        from . import graph
        return getattr(graph, name)
    raise AttributeError(name)
