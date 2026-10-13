"""Market evaluation node using the common v2 worker input/output contract."""

from .agent import (
    MarketAgent,
    MarketNode,
    build_market_graph,
    market_agent_node,
    run_market,
)
from .contract import Dataset, WorkerInput, WorkerResult
from .schema import AnalysisEnvelope, MarketAgentOutput, ReviewRequest

market_node = market_agent_node

__all__ = [
    "AnalysisEnvelope",
    "Dataset",
    "MarketAgent",
    "MarketAgentOutput",
    "MarketNode",
    "ReviewRequest",
    "WorkerInput",
    "WorkerResult",
    "build_market_graph",
    "market_agent_node",
    "market_node",
    "run_market",
]
