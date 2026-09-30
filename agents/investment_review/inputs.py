"""06 실행 입력: 기업 목록과 저장된 분석 결과 불러오기.

01 정규화 단계가 아직 없어 06이 최소한으로 대신한다.
- company_id: 02 임상과 같은 규칙 (CSV 기업명 첫 등장 순서 → c001, c002, ...)
- risk_company_id: 05 Risk는 frozen manifest의 자체 ID를 쓰므로 기업명으로 연결한다.
저장된 결과를 미리 넣으면 그래프는 그 Agent를 다시 실행하지 않고 보완 요청이 있을 때만 호출한다.
"""
from __future__ import annotations

import csv
import glob
import json
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "data/startup_list.csv"
RISK_MANIFEST = ROOT / "agents/risk/data/frozen_manifest.json"


def norm_name(name: Optional[str]) -> str:
    return re.sub(r"\s|\(주\)|주식회사", "", name or "")


def _risk_ids(manifest: Path) -> dict[str, str]:
    """정규화 기업명 → Risk company_id."""
    if not manifest.exists():
        return {}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    out = {}
    for cid, entry in data["companies"].items():
        state = json.loads((manifest.parent / entry["path"]).read_text(encoding="utf-8"))
        out[norm_name(state["company_profile"]["company_name"])] = cid
    return out


def load_companies(csv_path: Path = DEFAULT_CSV, risk_manifest: Path = RISK_MANIFEST) -> list[dict]:
    """CSV → 기업 프로필 목록. 중복 기업명은 마지막 행을 쓴다(02 임상과 같은 규칙)."""
    rows: dict[str, dict] = {}
    order: list[str] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("기업명") or "").strip()
            if not name:
                continue
            if norm_name(name) not in rows:
                order.append(norm_name(name))
            rows[norm_name(name)] = row
    risk = _risk_ids(risk_manifest)
    companies = []
    for i, key in enumerate(order, start=1):
        row = rows[key]
        profile = {**row, "company_id": f"c{i:03d}", "company_name": row["기업명"].strip(),
                   "legal_name": row["기업명"].strip()}
        if key in risk:
            profile["risk_company_id"] = risk[key]
        companies.append(profile)
    return companies


# ── 저장된 결과 (State 키별, company_id 기준) ──────────────────────────────
def saved_clinical(result_dir: Path) -> dict[str, dict]:
    """agents/clinical_regulatory/clinical_results/*.json → {company_id: Envelope}."""
    out = {}
    for f in glob.glob(str(Path(result_dir) / "*.json")):
        env = json.loads(Path(f).read_text(encoding="utf-8"))
        out[env["company_id"]] = env
    return out


def saved_risk(run_dir: Path, companies: list[dict]) -> dict[str, dict]:
    """agents/risk/data/full_*/<hash>/analysis_verified.json → {company_id: {risk_analysis, references}}."""
    by_name = {}
    for f in glob.glob(str(Path(run_dir) / "*" / "analysis_verified.json")):
        risk = json.loads(Path(f).read_text(encoding="utf-8"))
        state = json.loads((Path(f).parent / "reviewed/state.json").read_text(encoding="utf-8"))
        used = {p["source_id"] for p in risk["passages"]}
        refs = [s for s in state["company_evidence"] if s["source_id"] in used]
        by_name[norm_name(risk["company_name"])] = {"risk_analysis": risk, "references": refs}
    return {c["company_id"]: by_name[norm_name(c["company_name"])]
            for c in companies if norm_name(c["company_name"]) in by_name}


def saved_json(path: Path) -> dict[str, dict]:
    """{company_id: Envelope} 형태의 JSON (예: 시장·실적 일괄 실행 결과)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
