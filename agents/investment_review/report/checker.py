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


PROMPT_NUMBERS = {"100", "60", "2", "20", "1"}   # 프롬프트가 허용한 기준값 (100점 만점·60점·2점, 고용 20%, 최근 2년·1년)
CODES = re.compile(r"SCORE_BELOW|CRITERION_BELOW|MISSING_EVIDENCE|UNRESOLVED_CONFLICT|ANALYSIS_FAILED|"
                   r"\bRF\d\b|\bG0\d\b|\bCL0\d\b|zero_filled|criterion|not_applicable")
ITEM_NUMBER = re.compile(r"\bC[1-6]\b")
RAW_NUMBER = re.compile(r"\d{6,}")  # 3.9억 원이 아니라 392858405 같은 날것 숫자


def check_narrative(narrative: dict, ctx: dict) -> tuple[dict, list[str]]:
    """LLM 문장을 항목별로 검증하고 통과한 것만 남긴다. 실패한 항목은 템플릿 문장을 쓴다.

    - 모든 문장: 입력에 없는 숫자 금지(프롬프트 기준값 예외), 코드·변수명 금지
    - 부적격 사유(reason:*): 항목 번호(C1~C6)도 금지
    - 참고문헌(ref:n): 번호가 출처 범위 안, 기관 보고서·웹페이지는 원문 URL 포함
    """
    from urllib.parse import unquote

    from .citations import kind
    allowed = _allowed_numbers(ctx) | PROMPT_NUMBERS
    selected = {c["company_id"] for c in ctx["selected"]}
    others = {o["company_id"] for o in ctx["others"]}
    refs = ctx["references"]
    clean, issues = {}, []
    for key, text in (narrative or {}).items():
        kind_, _, target = key.partition(":")
        valid_key = (key in ("summary", "criteria_guide") or (kind_ in ("select", "points") and target in selected)
                     or (kind_ == "reason" and target in others)
                     or (kind_ == "ref" and target.isdigit() and 1 <= int(target) <= len(refs)))
        if not valid_key:
            issues.append(f"{key}: 보고서 대상이 아닌 항목 제거")
            continue
        body = text or ""
        if kind_ == "ref":
            ref = refs[int(target) - 1]
            url = ref.get("url") or ""
            if kind(ref) != "paper" and url and url not in body and unquote(url) not in body:
                issues.append(f"{key}: 원문 URL 누락·변경 → 코드 서식 사용")
                continue
            body = body.replace(url, "").replace(unquote(url), "")  # URL 안 숫자는 검사 제외
        bad = [n for n in NUMBER.findall(body) if n not in allowed and n.replace(",", "") not in allowed]
        if bad:
            issues.append(f"{key}: 데이터에 없는 숫자 {bad[:5]} → 템플릿 사용")
            continue
        if CODES.search(body) or (kind_ == "reason" and ITEM_NUMBER.search(body)):
            issues.append(f"{key}: 코드·항목 번호 포함 → 템플릿 사용")
            continue
        if RAW_NUMBER.search(body):
            issues.append(f"{key}: 읽기 어려운 날것 숫자 → 템플릿 사용")
            continue
        if key == "summary" and (missing := [c["name"] for c in ctx["selected"] if c["name"] not in body]):
            issues.append(f"summary: 선정 기업 {missing} 누락 → 템플릿 사용")
            continue
        clean[key] = text
    return clean, issues


def check_report(markdown: str, ctx: dict, pdf_pages: int | None = None) -> dict:
    """pdf_pages가 있으면 실제 PDF 쪽수로, 없으면 줄 너비 추정으로 5쪽을 검사한다."""
    issues = []
    pages = pdf_pages if pdf_pages is not None else estimate_pages(markdown)
    if pages > MAX_PAGES:
        issues.append(f"분량 {pages}쪽{'' if pdf_pages is not None else ' 추정'} (> {MAX_PAGES}쪽)")
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
    return {"pages": pages, "pages_source": "pdf" if pdf_pages is not None else "estimate",
            "summary_lines": summary_lines, "references": n_refs, "issues": issues, "ok": not issues}
