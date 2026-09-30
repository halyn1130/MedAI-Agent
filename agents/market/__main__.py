"""Command-line entry point for the market and business viability agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rag.market.retriever import EmptyRetriever

from .agent import MarketAgent, default_market_agent
from .providers import ConservativeMarketReasoner, DisabledWebSearchProvider


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="company profile or shared-state JSON"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="disable web, vector retrieval, and LLM calls; unknown is returned instead of fabricated scores",
    )
    args = parser.parse_args()

    payload = _read_json(args.input)
    profile = payload.get("company_profile") or payload
    agent = (
        MarketAgent(
            DisabledWebSearchProvider(), EmptyRetriever(), ConservativeMarketReasoner()
        )
        if args.demo
        else default_market_agent()
    )
    output = agent.run(
        profile,
        as_of=payload.get("as_of"),
        run_id=payload.get("run_id"),
        review_request=payload.get("review_request"),
        previous=payload.get("previous_analysis") or payload.get("market_analysis"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        json.dump(output.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
    print(f"market analysis written to {args.output}")


if __name__ == "__main__":
    main()
