"""PDF loader that preserves page-level provenance and dataset metadata."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PageDocument:
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DatasetStats:
    pdf_files: int
    physical_pages: int
    indexed_pages: int
    empty_text_pages: int = 0


def iter_pdf_paths(dataset_dir: Path) -> list[Path]:
    return sorted(path for path in dataset_dir.rglob("*.pdf") if path.is_file())


def _stable_document_id(relative_path: str) -> str:
    return (
        "market-doc-" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
    )


def _metadata_rows(dataset_dir: Path) -> dict[str, dict[str, str]]:
    path = dataset_dir / "metadata.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        key = (row.get("path") or row.get("filename") or "").strip().replace("\\", "/")
        if key:
            result[key] = {k: (v or "").strip() for k, v in row.items() if k}
    return result


def parse_page_spec(value: str | None) -> set[int]:
    """Parse a 1-based page spec such as ``1-3,7,10-12``."""
    pages: set[int] = set()
    if not value:
        return pages
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start < 1 or end < start:
                raise ValueError(f"invalid page range: {token}")
            pages.update(range(start, end + 1))
        else:
            page = int(token)
            if page < 1:
                raise ValueError(f"invalid page number: {token}")
            pages.add(page)
    return pages


def _row_for(
    path: Path, dataset_dir: Path, rows: dict[str, dict[str, str]]
) -> dict[str, str]:
    relative = path.relative_to(dataset_dir).as_posix()
    return rows.get(relative) or rows.get(path.name) or {}


def _selected_pages(page_count: int, row: dict[str, str]) -> set[int]:
    include = parse_page_spec(row.get("include_pages"))
    exclude = parse_page_spec(row.get("exclude_pages"))
    selected = include or set(range(1, page_count + 1))
    return {
        page for page in selected if 1 <= page <= page_count and page not in exclude
    }


def count_dataset_pages(dataset_dir: Path) -> DatasetStats:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised in dependency checks
        raise RuntimeError(
            "pypdf is required to inspect the market RAG dataset"
        ) from exc

    rows = _metadata_rows(dataset_dir)
    physical = 0
    indexed = 0
    paths = iter_pdf_paths(dataset_dir)
    for path in paths:
        reader = PdfReader(str(path))
        count = len(reader.pages)
        physical += count
        indexed += len(_selected_pages(count, _row_for(path, dataset_dir, rows)))
    return DatasetStats(
        pdf_files=len(paths), physical_pages=physical, indexed_pages=indexed
    )


def load_pdf_pages(dataset_dir: Path) -> tuple[list[PageDocument], DatasetStats]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pypdf is required to parse the market RAG dataset") from exc

    rows = _metadata_rows(dataset_dir)
    documents: list[PageDocument] = []
    physical = indexed = empty = 0
    paths = iter_pdf_paths(dataset_dir)
    for path in paths:
        relative = path.relative_to(dataset_dir).as_posix()
        row = _row_for(path, dataset_dir, rows)
        reader = PdfReader(str(path))
        physical += len(reader.pages)
        selected = _selected_pages(len(reader.pages), row)
        indexed += len(selected)
        pdf_title = str((reader.metadata or {}).get("/Title") or "").strip()
        document_id = row.get("document_id") or _stable_document_id(relative)
        base_metadata: dict[str, Any] = {
            "document_id": document_id,
            "title": row.get("title") or pdf_title or path.stem,
            "publisher": row.get("publisher") or "unknown",
            "published_at": row.get("published_at") or "",
            "region": row.get("region") or "",
            "segment": row.get("segment") or "healthcare_ai",
            "source_type": row.get("source_type") or "industry_report",
            "source_url": row.get("source_url") or relative,
            "path": relative,
        }
        for page_number, page in enumerate(reader.pages, start=1):
            if page_number not in selected:
                continue
            text = (page.extract_text() or "").strip()
            if not text:
                empty += 1
                continue
            documents.append(
                PageDocument(text=text, metadata={**base_metadata, "page": page_number})
            )
    stats = DatasetStats(
        pdf_files=len(paths),
        physical_pages=physical,
        indexed_pages=indexed,
        empty_text_pages=empty,
    )
    return documents, stats


def require_page_budget(
    stats: DatasetStats, max_pages: int, *, allow_over_limit: bool = False
) -> None:
    if stats.indexed_pages > max_pages and not allow_over_limit:
        raise ValueError(
            f"market RAG dataset has {stats.indexed_pages} indexed pages; "
            f"the configured limit is {max_pages}. Use metadata.csv exclude_pages/include_pages "
            "or pass --allow-over-limit explicitly."
        )


def metadata_template_rows(
    paths: Iterable[Path], dataset_dir: Path
) -> list[dict[str, str]]:
    return [
        {
            "path": path.relative_to(dataset_dir).as_posix(),
            "document_id": _stable_document_id(
                path.relative_to(dataset_dir).as_posix()
            ),
            "title": path.stem,
            "publisher": "",
            "published_at": "",
            "region": "",
            "segment": "healthcare_ai",
            "source_type": "industry_report",
            "source_url": "",
            "include_pages": "",
            "exclude_pages": "",
        }
        for path in paths
    ]
