"""Package existing market PDFs and metadata offline; never called by the worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

from rag.market.loader import iter_pdf_paths, load_pdf_pages

from .config import PROJECT_ROOT
from .contract import Dataset, WorkerInput, WorkerResult


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def package_dataset(
    dataset_dir: Path, output: Path, *, version="market-dataset-v1"
) -> dict:
    """Export stored pages to dataset.records and include the unchanged original PDFs."""
    dataset_dir, output = Path(dataset_dir).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Archive already exists: {output}")
    pages, stats = load_pdf_pages(dataset_dir)
    if not pages:
        raise ValueError("No text pages in the existing market dataset")
    sources, records = {}, []
    for page in pages:
        meta = page.metadata
        sid = meta["document_id"]
        sources.setdefault(
            sid,
            {
                "source_id": sid,
                "document_id": sid,
                "title": meta["title"],
                "url": meta["source_url"],
                "publisher": meta["publisher"],
                "source_type": meta["source_type"],
                "published_at": meta["published_at"] or None,
                "checked_at": None,
                "region": meta["region"],
                "segment": meta["segment"],
                "path": meta["path"],
            },
        )
        records.append(
            {
                "record_id": f"{sid}:page-{meta['page']}",
                "agent": "market",
                "source_id": sid,
                "text": page.text,
                "page": meta["page"],
                "locator": f"{meta['path']} page {meta['page']}",
                "geography": meta["region"],
                "segment": meta["segment"],
                "published_at": meta["published_at"] or None,
            }
        )
    dataset = Dataset(version=version, sources=list(sources.values()), records=records)
    entries = {
        "dataset.json": _json(dataset.model_dump(mode="json")),
        "worker_input.schema.json": _json(WorkerInput.model_json_schema()),
        "worker_result.schema.json": _json(WorkerResult.model_json_schema()),
        "README.md": (
            "# 시장 평가용 기존 데이터셋\n\n"
            "PDF 원본과 metadata.csv는 data/market_rag/ 아래에 있습니다.\n"
            "dataset.json은 공통 Worker 입력의 dataset 값으로 사용할 수 있습니다.\n"
            "Orchestrator에서 기업 평가에 필요한 records를 준비하여 전달하세요.\n"
            "시장 Worker는 PDF/인덱스를 읽거나 외부 검색·수집을 수행하지 않습니다.\n"
            "checked_at=null은 기존 메타데이터에 확인일이 없음을 뜻합니다.\n"
            "이번 내보내기에서는 자료를 다시 수집하지 않았습니다.\n"
        ).encode("utf-8"),
    }
    for pdf in iter_pdf_paths(dataset_dir):
        entries[f"data/market_rag/{pdf.relative_to(dataset_dir).as_posix()}"] = (
            pdf.read_bytes()
        )
    metadata = dataset_dir / "metadata.csv"
    if metadata.exists():
        entries["data/market_rag/metadata.csv"] = metadata.read_bytes()
    manifest = {
        "dataset_version": version,
        "source_count": len(sources),
        "record_count": len(records),
        "page_stats": asdict(stats),
        "collection_performed": False,
        "files": [
            {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(entries.items())
        ],
    }
    entries["manifest.json"] = _json(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output.parent, suffix=".zip.tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, raw in sorted(entries.items()):
                archive.writestr(name, raw)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=PROJECT_ROOT / "data/market_rag"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="market-dataset-v1")
    args = parser.parse_args()
    manifest = package_dataset(args.data_dir, args.output, version=args.version)
    print(
        f"{args.output}: {manifest['source_count']} sources, {manifest['record_count']} records"
    )


if __name__ == "__main__":
    main()
