"""Report the physical and selected page counts for the market RAG dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

from agents.market.config import MarketConfig

from .loader import count_dataset_pages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args()
    dataset_dir = args.data_dir or MarketConfig.from_env().dataset_dir
    stats = count_dataset_pages(dataset_dir)
    print(f"dataset_dir: {dataset_dir}")
    print(f"pdf_files: {stats.pdf_files}")
    print(f"physical_pages: {stats.physical_pages}")
    print(f"indexed_pages: {stats.indexed_pages}")
    print(f"within_limit: {stats.indexed_pages <= args.max_pages}")


if __name__ == "__main__":
    main()
