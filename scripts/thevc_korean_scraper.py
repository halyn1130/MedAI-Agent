#!/usr/bin/env python3
"""Scrape Korean startup candidates from THE VC's startup browse page.

This script is intended for the initial candidate-generation phase of the
healthcare AI investment evaluation project. It collects Korean startups from
The VC with visible fields like company name, industry, stage, and region, then
exports them to CSV for a later manual filter.

Install:
    . .venv/bin/activate
    python -m pip install playwright
    python -m playwright install chromium

Run:
    python scripts/thevc_korean_scraper.py --output data/thevc_korean_candidates.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright

TARGET_URL = (
    "https://thevc.kr/browse/startups?f="
    "N4IgZiBcoHYIYBcCWB7eAbJCCeVQqgG0RAdVcFtakAXQF8AaEAZwUQFcG8QDJjAF5cFHRkPUCWq4EWewC6rgBoGq9AB5QEAJxYBTOiADWy7AHcUCgCYcuxQBrjgGY7BIQDergEqHAAPOAW0cuAVLsAorYBu5yyMALY5cAjkxb0JoCaq1TU4UA"
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_company(value: str) -> str:
    text = normalize_text(value)
    return text.replace("기업 로고", "").strip()


def fetch_page_rows(url: str, max_pages: int = 3) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1800})
        page.goto(url, wait_until="networkidle", timeout=120000)

        for page_index in range(max_pages):
            page.locator("tr").first.wait_for(timeout=30000)
            tr_list = page.locator("tr")
            count = tr_list.count()

            for idx in range(count):
                row = tr_list.nth(idx)
                cells = row.locator("td").all_inner_texts()
                if len(cells) < 11:
                    continue

                company = clean_company(cells[1])
                if not company or company in seen:
                    continue

                item = {
                    "company_name": company,
                    "founded_on": normalize_text(cells[2].split(" ")[0]) if cells[2] else "",
                    "region": normalize_text(cells[3]) if len(cells) > 3 else "",
                    "product_service": normalize_text(cells[4]) if len(cells) > 4 else "",
                    "industry": normalize_text(cells[5]) if len(cells) > 5 else "",
                    "subindustry": normalize_text(cells[6]) if len(cells) > 6 else "",
                    "tech": normalize_text(cells[7]) if len(cells) > 7 else "",
                    "product_type": normalize_text(cells[8]) if len(cells) > 8 else "",
                    "latest_stage": normalize_text(cells[10]) if len(cells) > 10 else "",
                    "latest_investment_date": normalize_text(cells[11]) if len(cells) > 11 else "",
                    "source_url": url,
                }
                rows.append(item)
                seen.add(company)

            # try to click next page if available; if not, break
            next_button = page.locator("button:has-text('다음')").first
            if not next_button.is_visible():
                break
            try:
                next_button.click()
                page.wait_for_timeout(1500)
            except Exception:
                break

        browser.close()

    return rows


def save_csv(path: str, rows: List[Dict[str, str]]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fieldnames = [
        "company_name",
        "founded_on",
        "region",
        "product_service",
        "industry",
        "subindustry",
        "tech",
        "product_type",
        "latest_stage",
        "latest_investment_date",
        "source_url",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Korean startup candidates from THE VC")
    parser.add_argument("--output", default="data/thevc_korean_candidates.csv", help="CSV file path")
    parser.add_argument("--max-pages", type=int, default=3, help="Number of pages to iterate through")
    parser.add_argument("--url", default=TARGET_URL, help="THE VC startup browse URL")
    args = parser.parse_args()

    rows = fetch_page_rows(args.url, max_pages=args.max_pages)
    save_csv(args.output, rows)

    print(f"Collected {len(rows)} Korean startup rows")
    print(f"Saved to: {args.output}")
    for row in rows[:10]:
        print(f"- {row['company_name']} | {row['industry']} | {row['latest_stage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
