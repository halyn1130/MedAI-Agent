"""Evaluate supplied market worker_input JSON once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import MarketNode, default_market_agent


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
        "--demo",
        action="store_true",
        help="evaluate without LLM calls; unconfirmed scores remain unknown",
    )
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    node = MarketNode() if args.demo else default_market_agent()
    output = node(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"market evaluation written to {args.output}")


if __name__ == "__main__":
    main()
