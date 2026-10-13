"""Shared v2 contract. Workers evaluate once; the orchestrator owns scheduling."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "2.0"
CRITERIA_VERSION = "proposed-2.0"
AgentName = Literal["market"]
CRITERIA = {"market": ["C2", "C3", "C4"]}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dataset(Contract):
    version: str = Field(min_length=1)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    records: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_ids(self):
        ids = [s.get("source_id") for s in self.sources]
        if any(not isinstance(sid, str) or not sid for sid in ids):
            raise ValueError("Every dataset source requires a source_id")
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate dataset source_id")
        return self


class WorkerInput(Contract):
    run_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: str
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    criteria_version: str = Field(default=CRITERIA_VERSION, min_length=1)
    company_profile: dict[str, Any]
    dataset: Dataset
    previous_result: dict[str, Any] | None = None
    review_request: dict[str, Any] | None = None

    @model_validator(mode="after")
    def identity(self):
        if date.fromisoformat(self.as_of).isoformat() != self.as_of:
            raise ValueError("as_of must use YYYY-MM-DD")
        if self.company_profile.get("company_id", self.company_id) != self.company_id:
            raise ValueError("company_profile company_id mismatch")
        if self.previous_result:
            if self.previous_result.get("company_id") != self.company_id:
                raise ValueError("previous_result company_id mismatch")
            version = self.previous_result.get("result_version", 0)
            if type(version) is not int or version < 1:
                raise ValueError("previous_result requires a positive result_version")
        if self.review_request:
            if (
                self.review_request.get("company_id", self.company_id)
                != self.company_id
            ):
                raise ValueError("review_request company_id mismatch")
            if not self.review_request.get("request_id"):
                raise ValueError("review_request requires request_id")
        return self


class MissingItem(Contract):
    missing_id: str
    item: str
    reason: str
    related_criterion_ids: list[str] = Field(default_factory=list)
    required_for_assessment: bool = True


class WorkerResult(Contract):
    run_id: str
    company_id: str
    as_of: str
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    criteria_version: str = CRITERIA_VERSION
    agent: AgentName
    dataset_version: str = Field(min_length=1)
    result_version: int = Field(ge=1)
    analysis_status: Literal["complete", "partial", "failed"]
    data: dict[str, Any] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    coverage: list[dict[str, Any]] = Field(default_factory=list)
    review_responses: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def links(self):
        date.fromisoformat(self.as_of)
        for rows, key in (
            (self.sources, "source_id"),
            (self.evidence, "evidence_id"),
            (self.findings, "finding_id"),
        ):
            ids = [row.get(key) for row in rows]
            if any(not isinstance(i, str) or not i for i in ids) or len(
                set(ids)
            ) != len(ids):
                raise ValueError(f"Missing or duplicate {key}")
        sources = {s["source_id"] for s in self.sources}
        evidence = {e["evidence_id"] for e in self.evidence}
        for e in self.evidence:
            if not e.get("source_ids") or not set(e["source_ids"]) <= sources:
                raise ValueError("Evidence must reference returned sources")
            if e.get("company_id", self.company_id) != self.company_id:
                raise ValueError("Evidence company_id mismatch")
        for f in self.findings:
            if not set(f.get("evidence_ids", [])) <= evidence:
                raise ValueError("Finding references unknown evidence")
        return self


def parse_input(value: WorkerInput | dict, agent: AgentName) -> WorkerInput:
    request = WorkerInput.model_validate(
        deepcopy(value.model_dump() if isinstance(value, WorkerInput) else value)
    )
    if request.previous_result and request.previous_result.get("agent") != agent:
        raise ValueError("previous_result agent mismatch")
    if (
        request.review_request
        and request.review_request.get("target_agent", agent) != agent
    ):
        raise ValueError("review_request target_agent mismatch")
    return request


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return prefix + digest[:16]


def record_text(record: dict) -> str:
    for key in ("content", "text", "excerpt", "statement"):
        if isinstance(record.get(key), str) and record[key].strip():
            return record[key]
    return (
        json.dumps(record.get("record", {}), ensure_ascii=False)
        if record.get("record")
        else ""
    )


def provided_dataset(
    request: WorkerInput, agent: AgentName
) -> tuple[list[dict], list[dict], list[dict]]:
    """Copy supplied material, filtering other companies/workers and future material.

    This does not open files, dereference URLs, query an index, or use prior results.
    """
    errors, sources, records = [], {}, []
    type_aliases = {
        "official_record": "official",
        "company_announcement": "company",
        "partner_announcement": "partner",
        "industry_report": "research",
        "paper": "research",
    }
    for raw in request.dataset.sources:
        if raw.get("company_id") not in (None, request.company_id):
            continue
        if str(raw.get("published_at") or "")[:10] > request.as_of:
            continue
        src = deepcopy(raw)
        src.setdefault("title", src.get("url") or src["source_id"])
        src.setdefault("url", src["source_id"])
        src.setdefault("publisher", "unknown")
        src.setdefault("checked_at", request.as_of)
        kind = type_aliases.get(src.get("source_type"), src.get("source_type", "other"))
        src["source_type"] = (
            kind
            if kind in ("official", "company", "partner", "research", "news", "other")
            else "other"
        )
        sources[src["source_id"]] = src
    for index, raw in enumerate(request.dataset.records):
        if raw.get("company_id") not in (None, request.company_id) or raw.get(
            "agent"
        ) not in (None, agent):
            continue
        if any(
            str(raw.get(k) or "")[:10] > request.as_of
            for k in ("published_at", "event_date")
        ):
            continue
        rec = deepcopy(raw)
        ids = rec.get("source_ids") or (
            [rec["source_id"]] if rec.get("source_id") else []
        )
        if not ids:
            errors.append(
                {
                    "stage": "dataset",
                    "code": "record_without_source",
                    "record_index": index,
                }
            )
            continue
        unknown = set(ids) - {s["source_id"] for s in request.dataset.sources}
        if unknown:
            errors.append(
                {"stage": "dataset", "code": "unknown_source_id", "record_index": index}
            )
            continue
        if not set(ids) <= sources.keys():
            continue
        rec["source_ids"] = list(dict.fromkeys(ids))
        records.append(rec)
    return list(sources.values()), records, errors


def record_evidence(
    request: WorkerInput, agent: AgentName, record: dict, **extra
) -> dict:
    text = record_text(record)
    return {
        "evidence_id": stable_id(f"{request.company_id}:{agent}:ev-", [record, extra]),
        "company_id": request.company_id,
        "statement": record.get("statement") or text,
        "source_ids": record["source_ids"],
        "excerpt": record.get("excerpt") or text,
        "locator": record.get("locator"),
        "event_date": record.get("event_date"),
        "evidence_status": record.get("evidence_status", "unverified"),
        **extra,
    }


def missing_item(
    request: WorkerInput,
    agent: AgentName,
    item: str,
    reason: str,
    criteria: list[str] | None = None,
    required: bool = True,
) -> dict:
    return {
        "missing_id": stable_id(f"{request.company_id}:{agent}:mi-", item),
        "item": item,
        "reason": reason,
        "related_criterion_ids": criteria if criteria is not None else CRITERIA[agent],
        "required_for_assessment": required,
    }


def make_result(
    request: WorkerInput,
    agent: AgentName,
    *,
    data: dict,
    sources=None,
    evidence=None,
    findings=None,
    missing=None,
    coverage=None,
    errors=None,
    failed: bool = False,
) -> dict:
    """Stamp authoritative metadata and respond without deciding whether to run again."""
    sources, evidence, findings = sources or [], evidence or [], findings or []
    missing, coverage, errors = missing or [], coverage or [], errors or []
    version = (request.previous_result or {}).get("result_version", 0) + 1
    responses = []
    if request.review_request:
        prior = request.previous_result or {}
        before = {f["finding_id"]: f for f in prior.get("findings", [])}
        changed = [
            f["finding_id"] for f in findings if before.get(f["finding_id"]) != f
        ]
        responses.append(
            {
                "response_id": f"{request.company_id}:{agent}:rr-v{version}",
                "request_id": request.review_request["request_id"],
                "company_id": request.company_id,
                "result_version": version,
                "resolution": "unresolved",
                "change_type": "interpretation_updated" if changed else "unchanged",
                "updated_finding_ids": changed,
                "change_summary": f"전달된 데이터로 평가 완료; 변경된 finding {len(changed)}개",
                "remaining_unknowns": [m["item"] for m in missing],
                "limitations": ["전달된 데이터만 평가했으며 추가 수집은 수행하지 않음"],
            }
        )
    return WorkerResult(
        run_id=request.run_id,
        company_id=request.company_id,
        as_of=request.as_of,
        schema_version=request.schema_version,
        criteria_version=request.criteria_version,
        agent=agent,
        dataset_version=request.dataset.version,
        result_version=version,
        analysis_status="failed"
        if failed
        else "partial"
        if missing or errors
        else "complete",
        data=data,
        sources=sources,
        evidence=evidence,
        findings=findings,
        missing_items=missing,
        coverage=coverage,
        review_responses=responses,
        errors=errors,
    ).model_dump(mode="json")
