"""Market and business viability analysis agent.

The package keeps its public surface deliberately small so the future top-level
graph can integrate it without importing implementation details.
"""

from .agent import MarketAgent, build_market_graph, market_agent_node, run_market
from .schema import AnalysisEnvelope, MarketAgentOutput, ReviewRequest

market_node = market_agent_node

__all__ = [
    "AnalysisEnvelope",
    "MarketAgent",
    "MarketAgentOutput",
    "ReviewRequest",
    "build_market_graph",
    "market_agent_node",
    "market_node",
    "run_market",
]
