"""External search and structured reasoning adapters.

Imports for optional live services are lazy so unit tests and offline graph
wiring do not require credentials or heavyweight SDKs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, TypeVar
from urllib.parse import urlparse

import requests
from pydantic import BaseModel

from .schema import (
    AssessmentCheck,
    CheckStatus,
    DimensionDraft,
    MarketScopeDraft,
    SupplementalDraft,
)


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    publisher: str
    content: str
    published_at: str | None = None


class WebSearchProvider(Protocol):
    def search(
        self, query: str, *, as_of: str, max_results: int = 5
    ) -> list[WebSearchResult]: ...


class TavilySearchProvider:
    """Direct-URL search adapter matching the search service used by team agents."""

    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None = None, *, timeout: int = 30):
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        if not self.api_key:
            raise ValueError("TAVILY_API_KEY is required")
        self.timeout = timeout

    def search(
        self, query: str, *, as_of: str, max_results: int = 5
    ) -> list[WebSearchResult]:
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "include_raw_content": True,
            "include_answer": False,
            "end_date": as_of,
        }
        try:
            response = requests.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json().get("results", [])
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise ProviderError("market web search failed") from exc
        results: list[WebSearchResult] = []
        for row in rows:
            url = str(row.get("url") or "")
            content = str(row.get("raw_content") or row.get("content") or "").strip()
            if urlparse(url).scheme not in ("http", "https") or not content:
                continue
            published_at = str(row.get("published_date") or "")[:10] or None
            results.append(
                WebSearchResult(
                    title=str(row.get("title") or url),
                    url=url,
                    publisher=urlparse(url).netloc.removeprefix("www."),
                    content=content[:20_000],
                    published_at=published_at,
                )
            )
        return results


class DisabledWebSearchProvider:
    def search(
        self, query: str, *, as_of: str, max_results: int = 5
    ) -> list[WebSearchResult]:
        return []


class MarketReasoner(Protocol):
    def supplement(
        self,
        profile: dict[str, Any],
        required_fields: list[str],
        sources: list[dict[str, Any]],
    ) -> SupplementalDraft: ...

    def define_scope(
        self, profile: dict[str, Any], supplemental: dict[str, Any], *, as_of: str
    ) -> MarketScopeDraft: ...

    def analyze_dimension(
        self,
        dimension: str,
        scope: dict[str, Any],
        profile: dict[str, Any],
        supplemental: dict[str, Any],
        evidence: list[dict[str, Any]],
        rubric: dict[str, Any],
        review_request: dict[str, Any] | None = None,
    ) -> DimensionDraft: ...


T = TypeVar("T", bound=BaseModel)


class OpenAIStructuredMarketReasoner:
    """Structured-output reasoner; facts must cite IDs supplied in the prompt."""

    def __init__(self, model: str | None = None):
        model = model or os.getenv("MARKET_MODEL")
        if not model or not os.getenv("OPENAI_API_KEY"):
            raise ValueError("MARKET_MODEL and OPENAI_API_KEY are required")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "langchain-openai is required for live market analysis"
            ) from exc
        self.chat = ChatOpenAI(model=model, temperature=0, timeout=90, max_retries=1)

    def _invoke(self, schema: type[T], system: str, payload: dict[str, Any]) -> T:
        try:
            structured = self.chat.with_structured_output(schema)
            result = structured.invoke(
                [
                    ("system", system),
                    ("human", json.dumps(payload, ensure_ascii=False, default=str)),
                ]
            )
            return (
                result if isinstance(result, schema) else schema.model_validate(result)
            )
        except Exception as exc:
            raise ProviderError(
                f"market reasoning failed for {schema.__name__}"
            ) from exc

    def supplement(
        self,
        profile: dict[str, Any],
        required_fields: list[str],
        sources: list[dict[str, Any]],
    ) -> SupplementalDraft:
        system = """당신은 Healthcare AI 기업 정보 검증 담당자다.
주어진 원문 검색 결과에서 requested_fields에 해당하는 기업별 사실만 추출한다.
각 fact는 반드시 제공된 source_id를 인용하고 가능한 경우 원문 그대로의 짧은 excerpt를 포함한다.
검색 엔진이나 AI의 요약을 독립 근거로 만들지 말고, 출처에 없는 내용은 추측하지 않는다.
확인하지 못한 필드는 unresolved_fields에 넣는다. 한국어로 작성한다."""
        draft = self._invoke(
            SupplementalDraft,
            system,
            {
                "company_profile": profile,
                "requested_fields": required_fields,
                "sources": sources,
            },
        )
        allowed = {source["source_id"] for source in sources}
        draft.facts = [
            fact
            for fact in draft.facts
            if fact.source_ids and set(fact.source_ids) <= allowed
        ]
        found = {fact.field for fact in draft.facts}
        draft.unresolved_fields = sorted(
            set(draft.unresolved_fields) | (set(required_fields) - found)
        )
        return draft

    def define_scope(
        self, profile: dict[str, Any], supplemental: dict[str, Any], *, as_of: str
    ) -> MarketScopeDraft:
        system = """당신은 Healthcare AI 시장 분류 담당자다.
기업의 제품 기능, intended use, 실제 고객·구매자, 해결 문제와 지역을 기준으로 비교 가능한 세부시장을 정의한다.
의료영상이나 병원 시장으로 고정하지 않는다. 공개되지 않은 내용은 assumptions에 명시한다.
segment는 지나치게 넓은 'Healthcare AI'가 아니라 자료 검색과 비교가 가능한 동질 시장으로 정한다.
base_year는 분석 기준일의 연도, forecast_period는 기본적으로 그 연도부터 5년 뒤까지로 둔다."""
        return self._invoke(
            MarketScopeDraft,
            system,
            {
                "company_profile": profile,
                "supplemental_context": supplemental,
                "as_of": as_of,
            },
        )

    def analyze_dimension(
        self,
        dimension: str,
        scope: dict[str, Any],
        profile: dict[str, Any],
        supplemental: dict[str, Any],
        evidence: list[dict[str, Any]],
        rubric: dict[str, Any],
        review_request: dict[str, Any] | None = None,
    ) -> DimensionDraft:
        system = """당신은 Healthcare AI 시장·사업성 분석가다.
제공된 RAG evidence만 시장·산업 근거로 사용한다. evidence_id가 없는 주장은 점수 근거로 쓰지 않는다.
출처의 지역, 세그먼트, 기준연도와 전망기간이 다른 수치를 직접 비교하지 않는다.
자료 미발견은 부정 근거가 아니다. 판단 불가 체크는 unknown으로 유지한다.
다른 Agent의 책임인 임상 적정성, 규제 최종판단, 실제 매출·계약 실적, 운영 리스크를 판정하지 않는다.
시장 수치는 소수점 비율을 사용한다(12% CAGR = 0.12). 한국어로 작성한다."""
        draft = self._invoke(
            DimensionDraft,
            system,
            {
                "dimension": dimension,
                "market_scope": scope,
                "company_profile": profile,
                "supplemental_context": supplemental,
                "rubric": rubric,
                "evidence": evidence,
                "review_request": review_request,
            },
        )
        allowed = {item["evidence_id"] for item in evidence}
        draft.evidence_ids = [item for item in draft.evidence_ids if item in allowed]
        for check in draft.checks:
            check.evidence_ids = [
                item for item in check.evidence_ids if item in allowed
            ]
            if (
                check.status in (CheckStatus.YES, CheckStatus.NO)
                and not check.evidence_ids
            ):
                check.status = CheckStatus.UNKNOWN
                check.rationale = "제공된 근거 ID로 확인하지 못함"
        draft.market_metrics = [
            metric
            for metric in draft.market_metrics
            if set(metric.evidence_ids) <= allowed
        ]
        return draft


class ConservativeMarketReasoner:
    """Offline fallback that never invents facts or scores."""

    def supplement(
        self,
        profile: dict[str, Any],
        required_fields: list[str],
        sources: list[dict[str, Any]],
    ) -> SupplementalDraft:
        return SupplementalDraft(unresolved_fields=required_fields)

    def define_scope(
        self, profile: dict[str, Any], supplemental: dict[str, Any], *, as_of: str
    ) -> MarketScopeDraft:
        year = date.fromisoformat(as_of).year
        supplemental_values = {
            item.get("field"): item.get("value")
            for item in supplemental.get("facts", [])
            if isinstance(item, dict) and item.get("field")
        }

        def value(*names: str, default: Any = "unknown") -> Any:
            for name in names:
                if supplemental_values.get(name) not in (None, "", [], {}):
                    return supplemental_values[name]
            products = profile.get("products") or []
            containers = [profile]
            if products and isinstance(products[0], dict):
                containers.append(products[0])
            for container in containers:
                for name in names:
                    if container.get(name) not in (None, "", [], {}):
                        return container[name]
            return default

        def text(*names: str, default: str = "unknown") -> str:
            selected = value(*names, default=default)
            return (
                ", ".join(str(item) for item in selected)
                if isinstance(selected, list)
                else str(selected)
            )

        segment = text(
            "segment",
            "product_type",
            "대분류",
            default="unresolved healthcare AI segment",
        )
        geography = value("geography", "regions", default=["unknown"])
        if isinstance(geography, str):
            geography = [geography]
        return MarketScopeDraft(
            segment=segment,
            geography=geography,
            target_customer=text("target_customer"),
            buyer=text("buyer"),
            primary_user=text("primary_user", "user"),
            product_type=text("product_type"),
            intended_use=text("intended_use", "description"),
            core_problem=text("core_problem", "problem"),
            business_model=text("business_model", "pricing_model"),
            base_year=year,
            forecast_period=f"{year}-{year + 5}",
            assumptions=["오프라인 fallback: 모델 기반 시장 범위 검증을 수행하지 않음"],
        )

    def analyze_dimension(
        self,
        dimension: str,
        scope: dict[str, Any],
        profile: dict[str, Any],
        supplemental: dict[str, Any],
        evidence: list[dict[str, Any]],
        rubric: dict[str, Any],
        review_request: dict[str, Any] | None = None,
    ) -> DimensionDraft:
        return DimensionDraft(
            rationale="구조 실행은 완료했으나 분석 모델 없이 판단을 생성하지 않음",
            evidence_ids=[],
            checks=[
                AssessmentCheck(
                    check_id=item["check_id"],
                    status=CheckStatus.UNKNOWN,
                    rationale="분석 모델 미설정",
                )
                for item in rubric.get("checks", [])
            ],
            missing_fields=[dimension],
        )
