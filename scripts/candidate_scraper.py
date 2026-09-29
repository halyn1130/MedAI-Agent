#!/usr/bin/env python3
"""Healthcare AI startup candidate scraper.

This script searches the public web for startup candidates that match the initial
project filter:
- non-public company
- funding stage in Seed~Series C
- no completed exit / M&A outcome in evidence
- healthcare AI / medical AI relevance

It does not require API keys. It searches public web pages and saves a CSV of
candidate companies with the evidence and a confidence score.
"""

from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote_plus, urlparse, urlencode

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

QUERY_TEMPLATES = [
    "healthcare AI startup seed",
    "medical AI startup series A",
    "digital health AI startup series B",
    "clinical AI startup series C",
    "healthcare machine learning startup seed",
    "medical imaging AI startup",
    "AI healthcare startup funding",
    "digital health startup Series A AI",
]

STAGE_KEYWORDS = {
    "seed": ["seed"],
    "series a": ["series a", "series 1", "series-a"],
    "series b": ["series b", "series 2", "series-b"],
    "series c": ["series c", "series 3", "series-c"],
}

EXIT_KEYWORDS = [
    "acquired",
    "acquisition",
    "m&a",
    "merge",
    "merged",
    "exit",
    "exited",
    "buyout",
    "taken private",
]

PUBLIC_TRADE_KEYWORDS = [
    "nasdaq",
    "nyse",
    "public company",
    "listed on",
    "stock ticker",
    "ipo",
    "initial public offering",
]

HEALTHCARE_KEYWORDS = [
    "healthcare",
    "health care",
    "medical",
    "clinical",
    "hospital",
    "patient",
    "care delivery",
    "diagnostic",
    "imaging",
    "biotech",
    "digital health",
    "ai in healthcare",
    "medical ai",
    "health tech",
    "healthtech",
]

AI_KEYWORDS = [
    "artificial intelligence",
    "ai",
    "machine learning",
    "ml",
    "deep learning",
    "computer vision",
    "nlp",
    "llm",
    "genai",
]


class ScrapeError(RuntimeError):
    pass


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_url_from_duckduckgo(href: str) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http"):
        return href
    parsed = urlparse(href)
    if parsed.netloc == "":
        q = parse_qs(parsed.query)
        uddg = q.get("uddg")
        if uddg:
            return uddg[0]
        if "url=" in parsed.query:
            url = parse_qs(parsed.query).get("url")
            if url:
                return url[0]
    return href


def extract_url_from_bing(href: str) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http") and not href.startswith("https://www.bing.com"):
        return href
    parsed = urlparse(href)
    if parsed.netloc.startswith("www.bing.com"):
        query = parse_qs(parsed.query)
        if "u" in query:
            encoded = query["u"][0]
            for candidate in [encoded, encoded[2:], encoded[3:]]:
                try:
                    decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4), validate=False)
                    text = decoded.decode("utf-8", errors="ignore")
                    if text.startswith("http"):
                        return text
                except Exception:
                    continue
        if "q" in query:
            return query["q"][0]
    return href


def build_search_query(term: str, page: int = 1) -> str:
    base = "https://www.bing.com/search?q=" + quote_plus(term)
    if page > 1:
        base += f"&first={((page - 1) * 10) + 1}"
    return base


def fetch_html(url: str, timeout: int = 20) -> str:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_search_results(html: str) -> List[Tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: List[Tuple[str, str, str]] = []
    seen: Set[str] = set()

    # Bing organic result blocks
    for result in soup.select("li.b_algo"):
        link_tag = result.select_one("a")
        if not link_tag:
            continue
        raw_url = extract_url_from_bing(link_tag.get("href", ""))
        title = normalize_space(link_tag.get_text(" ", strip=True))
        text = normalize_space(result.get_text(" ", strip=True))
        if not raw_url or not title:
            continue
        if raw_url.startswith("https://www.bing.com") or raw_url.startswith("/search?"):
            continue
        if raw_url in seen:
            continue
        seen.add(raw_url)
        results.append((title, raw_url, text))

    if results:
        return results

    # Fallback: duckduckgo HTML layout
    for result in soup.select(".result"):
        link_tag = result.select_one("a.result-link")
        if not link_tag:
            continue
        raw_url = extract_url_from_duckduckgo(link_tag.get("href", ""))
        title = normalize_space(link_tag.get_text(" ", strip=True))
        snippet = ""
        snippet_tag = result.select_one(".result-snippet")
        if snippet_tag:
            snippet = normalize_space(snippet_tag.get_text(" ", strip=True))
        if not raw_url or not title:
            continue
        if raw_url in seen:
            continue
        seen.add(raw_url)
        results.append((title, raw_url, snippet))
    return results


def fetch_page_text(url: str) -> str:
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        if response.status_code >= 400:
            return ""
        text = response.text
    except Exception:
        return ""

    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return ""

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    return normalize_space(text)


def infer_funding_stage(text: str) -> Optional[str]:
    lowered = text.lower()
    for stage, variants in STAGE_KEYWORDS.items():
        if any(v in lowered for v in variants):
            return stage
    return None


def infer_healthcare_ai(text: str) -> bool:
    lowered = text.lower()
    has_healthcare = any(keyword in lowered for keyword in HEALTHCARE_KEYWORDS)
    has_ai = any(keyword in lowered for keyword in AI_KEYWORDS)
    return has_healthcare and has_ai


def infer_exit_status(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in EXIT_KEYWORDS)


def infer_public_status(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in PUBLIC_TRADE_KEYWORDS)


def extract_company_name(title: str, url: str) -> str:
    cleaned = title.strip()
    if cleaned:
        return cleaned
    parsed = urlparse(url)
    hostname = parsed.netloc.replace("www.", "")
    return hostname.split(".")[0].replace("-", " ").title()


def score_candidate(record: Dict) -> float:
    score = 0.0
    if record.get("healthcare_ai"):
        score += 30
    if record.get("funding_stage"):
        score += 25
    if record.get("likely_non_public"):
        score += 20
    if record.get("exit_status"):
        score -= 40
    if record.get("source_quality") == "strong":
        score += 10
    if record.get("page_text"):
        score += 10
    return round(max(0.0, min(score, 100.0)), 1)


def evaluate_candidate(title: str, url: str, snippet: str) -> Dict:
    text = fetch_page_text(url)

    healthcare_ai = infer_healthcare_ai(text or snippet)
    funding_stage = infer_funding_stage(text or snippet)
    exit_status = infer_exit_status(text or snippet)
    likely_non_public = not infer_public_status(text or snippet)
    source_quality = "strong" if text and len(text) > 300 else "weak"

    record = {
        "company_name": extract_company_name(title, url),
        "url": url,
        "title": title,
        "snippet": snippet,
        "stage": funding_stage,
        "healthcare_ai": healthcare_ai,
        "likely_non_public": likely_non_public,
        "exit_status": exit_status,
        "source_quality": source_quality,
        "page_text_length": len(text),
        "notes": "",
        "score": 0.0,
    }

    if not healthcare_ai:
        record["notes"] = "Healthcare/AI keywords not clearly detected."
    if funding_stage is None:
        record["notes"] += " Funding stage not clear from page text."
    if exit_status:
        record["notes"] += " Public exit or acquisition language detected."
    if not likely_non_public:
        record["notes"] += " Public stock listing likely detected."

    record["score"] = score_candidate(record)
    return record


def collect_candidates(queries: Iterable[str], max_results_per_query: int = 8) -> List[Dict]:
    all_candidates: Dict[str, Dict] = {}

    for query in queries:
        try:
            html = fetch_html(build_search_query(query))
        except Exception:
            continue

        search_results = extract_search_results(html)
        if not search_results:
            continue

        for title, url, snippet in search_results[:max_results_per_query]:
            if not url:
                continue
            if url.startswith("https://duckduckgo.com"):
                continue
            if url.startswith("https://www.google"):
                continue

            candidate = evaluate_candidate(title, url, snippet)
            key = candidate["company_name"].lower() + "::" + candidate["url"]
            if key in all_candidates:
                continue
            all_candidates[key] = candidate

    candidates = list(all_candidates.values())
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def filter_initial_candidates(candidates: List[Dict]) -> List[Dict]:
    filtered: List[Dict] = []
    for item in candidates:
        if item.get("exit_status"):
            continue
        if item.get("likely_non_public") is False:
            continue
        if item.get("healthcare_ai") or "healthcare" in (item.get("company_name", "") + " " + item.get("title", "")).lower():
            filtered.append(item)
            continue
        if item.get("stage") in {"seed", "series a", "series b", "series c"}:
            filtered.append(item)
    return filtered


def save_csv(path: str, rows: List[Dict]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fieldnames = [
        "company_name",
        "url",
        "title",
        "stage",
        "healthcare_ai",
        "likely_non_public",
        "exit_status",
        "score",
        "snippet",
        "notes",
        "page_text_length",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect candidate healthcare AI startups by funding stage")
    parser.add_argument("--output", default="data/candidates.csv", help="CSV path to write results")
    parser.add_argument("--max-results", type=int, default=10, help="Max search results per query")
    parser.add_argument("--max-pages", type=int, default=20, help="Max candidate rows to save")
    args = parser.parse_args()

    candidates = collect_candidates(QUERY_TEMPLATES, max_results_per_query=args.max_results)
    filtered = filter_initial_candidates(candidates)
    selected = filtered[: args.max_pages] if filtered else candidates[: args.max_pages]

    if not filtered:
        print("No strong candidates matched the initial filters; saved raw search results for manual review.", file=sys.stderr)

    save_csv(args.output, selected)

    print(f"Collected {len(selected)} candidate records.")
    print(f"Saved to: {args.output}")

    for item in selected:
        print(f"- {item['company_name']} | Stage: {item['stage']} | Score: {item['score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
