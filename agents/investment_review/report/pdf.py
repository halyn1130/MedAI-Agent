"""보고서 Markdown → HTML → PDF (A4). Playwright로 Chrome 인쇄 기능을 쓴다.

- 브라우저: 설치된 Google Chrome(channel="chrome")을 먼저 쓰고, 없으면 Playwright Chromium.
  Chromium이 없으면: python -m playwright install chromium
- 한글 글꼴은 시스템 글꼴(Apple SD Gothic Neo / Noto Sans KR / 맑은 고딕)을 쓴다.
- 만든 PDF의 실제 쪽수를 pypdf로 센다.
"""
from __future__ import annotations

import html
from pathlib import Path

from markdown_it import MarkdownIt

CSS = """
@page { size: A4; margin: 14mm 14mm 16mm 14mm; }
body { font-family: "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", sans-serif;
       font-size: 9pt; line-height: 1.45; color: #1f2937; }
h1 { font-size: 15pt; margin: 0 0 4px; color: #111827; }
h1 + p { color: #4b5563; margin-top: 0; }
h2 { font-size: 11.5pt; margin: 14px 0 6px; padding-bottom: 3px; border-bottom: 1.5px solid #1f2937; }
h3 { font-size: 10pt; margin: 10px 0 4px; }
h4 { font-size: 9pt; margin: 6px 0 2px; color: #374151; }
p, ul { margin: 3px 0; }
ul { padding-left: 16px; }
li { margin: 1px 0; }
table { border-collapse: collapse; width: 100%; margin: 4px 0 8px; font-size: 8pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #d1d5db; padding: 2px 5px; vertical-align: top; }
th { background: #f3f4f6; }
code { font-size: 8pt; background: #f3f4f6; padding: 0 2px; }
h2#reference + ol, .reference { font-size: 7.5pt; }
ol { padding-left: 18px; }
.reference li { word-break: break-all; }
"""

FOOTER = ('<div style="font-size:7pt;color:#6b7280;width:100%;text-align:center;">'
          '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')


def to_html(markdown: str, title: str = "투자 검토 보고서") -> str:
    body = MarkdownIt("commonmark").enable("table").render(markdown)
    # REFERENCE 목록은 작은 글씨·URL 줄바꿈
    body = body.replace("<h2>REFERENCE</h2>\n<ol>", '<h2 id="reference">REFERENCE</h2>\n<ol class="reference">')
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"<style>{CSS}</style></head><body>{body}</body></html>")


def page_count(path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


def to_pdf(markdown: str, path: Path) -> int:
    """PDF를 쓰고 실제 쪽수를 돌려준다. 브라우저를 못 띄우면 RuntimeError."""
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Error:
                continue
        if browser is None:
            raise RuntimeError("PDF용 브라우저 없음: Google Chrome 설치 또는 `python -m playwright install chromium`")
        try:
            page = browser.new_page()
            page.set_content(to_html(markdown), wait_until="load")
            page.pdf(path=str(path), format="A4", print_background=True, prefer_css_page_size=True,
                     display_header_footer=True, header_template="<div></div>", footer_template=FOOTER)
        finally:
            browser.close()
    return page_count(path)
