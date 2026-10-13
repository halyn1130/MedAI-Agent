"""Retrieve relevant PDF passages, then evaluate the prepared market input once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import MarketNode, default_market_agent
from .retrieval import MarketRagPreparationError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="common worker_input JSON, including dataset",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--prepared-input",
        action="store_true",
        help="skip RAG only when dataset already contains selected worker context",
    )
    parser.add_argument(
        "--prepared-output", type=Path,
        help="save the selected worker input for provenance and input-size inspection",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="evaluate without LLM calls; unconfirmed scores remain unknown",
    )
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    reasoner = None if args.demo else default_market_agent().reasoner
    node = MarketNode(reasoner, use_rag=not args.prepared_input)
    try:
        payload = node.prepare_input(payload)
    except MarketRagPreparationError as exc:
        parser.error(str(exc))
    if args.prepared_output:
        args.prepared_output.parent.mkdir(parents=True, exist_ok=True)
        args.prepared_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    output = node.evaluate_prepared(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"market evaluation written to {args.output}")


if __name__ == "__main__":
    main()
