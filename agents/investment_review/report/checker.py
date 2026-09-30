"""보고서 사후 검증.

- 분량: 5쪽 이내, SUMMARY 1/2쪽 이내 (Markdown 줄 너비로 추정한 근사치)
- 인용: 본문의 [n]이 REFERENCE에 있고, REFERENCE 항목은 본문에서 인용됨
- 누락: 전체 후보가 판정 표에 모두 있음
- LLM 서술: 보고서 데이터에 없는 숫자가 나오면 그 문장을 버린다
"""
from __future__ import annotations

import math
import re
import unicodedata

MAX_PAGES = 5
LINE_WIDTH = 100            # A4 한 줄 너비 (한글 2칸, 영문·숫자 1칸)
LINES_PER_PAGE = 50
SUMMARY_MAX_LINES = LINES_PER_PAGE // 2
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def estimate_lines(markdown: str) -> int:
    return sum(max(1, math.ceil(_width(line) / LINE_WIDTH)) for line in markdown.splitlines())


def estimate_pages(markdown: str) -> float:
    return round(estimate_lines(markdown) / LINES_PER_PAGE, 1)


def _section(markdown: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", markdown, re.M | re.S)
    return m.group(1) if m else ""


def _allowed_numbers(ctx: dict) -> set[str]:
    """context에 있는 모든 숫자의 표기 후보 (정수·소수 1자리·퍼센트·억 원)."""
    out: set[str] = set()

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            for x in (v, v * 100, v / 1e8, v / 1e6):
                out.update({f"{x:g}", f"{x:.0f}", f"{x:.1f}", f"{x:,.0f}", f"{x:,.1f}"})
        elif isinstance(v, str):
            out.update(NUMBER.findall(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(ctx)
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in out} | out


def check_narrative(narrative: dict, ctx: dict) -> tuple[dict, list[str]]:
    """데이터에 없는 숫자를 쓴 LLM 문장을 제거한다."""
    allowed = _allowed_numbers(ctx)
    ids = {c["company_id"] for c in ctx["selected"]}
    clean, issues = {}, []
    for key, text in (narrative or {}).items():
        if key != "summary" and key not in ids:
            issues.append(f"narrative: 선정 기업이 아닌 키 {key} 제거")
            continue
        bad = [n for n in NUMBER.findall(text or "") if n not in allowed and n.replace(",", "") not in allowed]
        if bad:
            issues.append(f"narrative[{key}]: 데이터에 없는 숫자 {bad[:5]} → 템플릿 문장 사용")
            continue
        clean[key] = text
    return clean, issues


def check_report(markdown: str, ctx: dict) -> dict:
    issues = []
    pages = estimate_pages(markdown)
    if pages > MAX_PAGES:
        issues.append(f"분량 {pages}쪽 추정 (> {MAX_PAGES}쪽)")
    summary_lines = estimate_lines(_section(markdown, "1. SUMMARY").strip())
    if summary_lines > SUMMARY_MAX_LINES:
        issues.append(f"SUMMARY {summary_lines}줄 추정 (> 1/2쪽 {SUMMARY_MAX_LINES}줄)")

    body, _, reference = markdown.partition("\n## REFERENCE")
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
    n_refs = len(ctx["references"])
    if missing := sorted(n for n in cited if n > n_refs):
        issues.append(f"REFERENCE에 없는 인용 번호 {missing}")
    if unused := sorted(set(range(1, n_refs + 1)) - cited):
        issues.append(f"본문에서 인용하지 않은 REFERENCE {unused}")

    table = _section(markdown, "3. 전체 후보 판정")
    names = [c["name"] for c in ctx["selected"]] + [o["name"] for o in ctx["others"]]
    if absent := [n for n in names if f"| {n} |" not in table]:
        issues.append(f"판정 표에 없는 기업 {absent}")
    return {"pages": pages, "summary_lines": summary_lines, "references": n_refs, "issues": issues, "ok": not issues}
