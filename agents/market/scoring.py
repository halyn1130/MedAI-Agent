"""Deterministic scoring for the proposed market criteria."""

from __future__ import annotations

import re

from .schema import (
    AssessmentCheck,
    CheckStatus,
    CriterionInput,
    DimensionDraft,
    DimensionResult,
    MarketCriteriaInputs,
    MarketDimensions,
    MarketScope,
    ScoreStatus,
)

RUBRICS = {
    "size_growth": {
        "criterion_id": "C2",
        "description": "동일 지역·세그먼트·기간의 CAGR과 시장 규모를 분리해 평가",
        "checks": [],
    },
    "demand": {
        "criterion_id": "C3",
        "description": "목표 고객·사용자의 미충족 수요와 구매 필요성",
        "checks": [
            {
                "check_id": "DM1",
                "description": "목표 고객·사용자에게 문제가 객관적으로 존재",
            },
            {"check_id": "DM2", "description": "건강·연구·운영 부담이 수치로 확인"},
            {"check_id": "DM3", "description": "목표 사용자가 해결 필요성을 명시"},
            {
                "check_id": "DM4",
                "description": "독립적인 2개 이상 고객·구매자에서 같은 수요 확인",
            },
            {
                "check_id": "DM5",
                "description": "구매 우선순위 또는 예산·지불 의사 확인",
            },
        ],
    },
    "commercialization": {
        "criterion_id": "C3",
        "description": "사업유형에 맞는 도입·상용화 조건",
        "checks": [
            {"check_id": "AD1", "description": "목표 업무 흐름과 사용 단계 적합성"},
            {"check_id": "AD2", "description": "기술·시스템 연동 경로"},
            {"check_id": "AD3", "description": "운영·교육 담당과 업무 분담"},
            {"check_id": "AD4", "description": "데이터·인프라 조건 충족 가능성"},
            {"check_id": "AD5", "description": "고객 구매·배포 절차"},
        ],
    },
    "monetization": {
        "criterion_id": "C4",
        "description": "사업유형에 맞는 지불·과금·반복 매출 구조",
        "checks": [
            {"check_id": "MO1", "description": "실제 지불 주체"},
            {"check_id": "MO2", "description": "가격·과금 단위"},
            {"check_id": "MO3", "description": "지불·조달 경로"},
            {"check_id": "MO4", "description": "갱신·반복 사용·재구매 구조"},
            {"check_id": "MO5", "description": "도입 비용 대비 경제성 근거"},
        ],
    },
}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", "", value.casefold())


def _normalized_geography(value: str) -> str:
    normalized = _normalized(value)
    aliases = {
        "kr": "kr",
        "korea": "kr",
        "southkorea": "kr",
        "republicofkorea": "kr",
        "대한민국": "kr",
        "한국": "kr",
        "us": "us",
        "usa": "us",
        "unitedstates": "us",
        "미국": "us",
        "global": "global",
        "worldwide": "global",
        "전세계": "global",
    }
    return aliases.get(normalized, normalized)


def _forecast_years(scope: MarketScope) -> tuple[int, int] | None:
    years = [
        int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", scope.forecast_period)
    ]
    return (years[0], years[1]) if len(years) >= 2 else None


def comparable_cagr_value(
    draft: DimensionDraft, scope: MarketScope | None
) -> float | None:
    """Return one CAGR only when scope matches and the values do not conflict."""

    period = _forecast_years(scope) if scope else None
    geographies = (
        {_normalized_geography(item) for item in scope.geography} if scope else set()
    )
    expected_segment = _normalized(scope.segment) if scope else ""
    values: list[float] = []
    for metric in draft.market_metrics:
        if metric.metric_type != "cagr":
            continue
        if scope:
            if _normalized(metric.segment) != expected_segment:
                continue
            if _normalized_geography(metric.geography) not in geographies:
                continue
            if period is None or (metric.start_year, metric.end_year) != period:
                continue
        value = metric.value
        if metric.unit.lower() in {"percent", "%", "percentage"}:
            value /= 100.0
        values.append(value)
    if not values or any(abs(value - values[0]) > 1e-9 for value in values[1:]):
        return None
    return values[0]


def _growth_score(value: float) -> int:
    if value < 0:
        return 0
    if value < 0.05:
        return 1
    if value < 0.10:
        return 2
    if value < 0.15:
        return 3
    if value < 0.20:
        return 4
    return 5


def _normalized_checks(
    dimension: str, checks: list[AssessmentCheck]
) -> list[AssessmentCheck]:
    expected = [item["check_id"] for item in RUBRICS[dimension]["checks"]]
    by_id = {check.check_id: check for check in checks if check.check_id in expected}
    return [
        by_id.get(
            check_id,
            AssessmentCheck(
                check_id=check_id,
                status=CheckStatus.UNKNOWN,
                rationale="평가 결과에 체크가 누락됨",
            ),
        )
        for check_id in expected
    ]


def score_dimension(
    dimension: str,
    draft: DimensionDraft,
    missing_item_ids: list[str],
    scope: MarketScope | None = None,
) -> DimensionResult:
    if draft.not_applicable:
        return DimensionResult(
            score=None,
            score_status=ScoreStatus.NOT_APPLICABLE,
            rationale=draft.rationale,
            evidence_ids=draft.evidence_ids,
            missing_item_ids=missing_item_ids,
            assumptions=draft.assumptions,
            checks=draft.checks,
            market_metrics=draft.market_metrics,
            business_concerns=draft.business_concerns,
        )

    if dimension == "size_growth":
        value = comparable_cagr_value(draft, scope)
        status = ScoreStatus.SCORED if value is not None else ScoreStatus.UNKNOWN
        return DimensionResult(
            score=float(_growth_score(value)) if value is not None else None,
            score_status=status,
            rationale=draft.rationale,
            evidence_ids=sorted(set(draft.evidence_ids)),
            missing_item_ids=missing_item_ids,
            assumptions=draft.assumptions,
            market_metrics=draft.market_metrics,
            business_concerns=draft.business_concerns,
        )

    checks = _normalized_checks(dimension, draft.checks)
    statuses = [check.status for check in checks]
    if statuses and all(status == CheckStatus.NOT_APPLICABLE for status in statuses):
        score_status = ScoreStatus.NOT_APPLICABLE
        score = None
    elif any(
        status in (CheckStatus.UNKNOWN, CheckStatus.NOT_APPLICABLE)
        for status in statuses
    ):
        score_status = ScoreStatus.UNKNOWN
        score = None
    else:
        score_status = ScoreStatus.SCORED
        score = float(sum(status == CheckStatus.YES for status in statuses))
    evidence_ids = sorted(
        set(draft.evidence_ids)
        | {evidence_id for check in checks for evidence_id in check.evidence_ids}
    )
    return DimensionResult(
        score=score,
        score_status=score_status,
        rationale=draft.rationale,
        evidence_ids=evidence_ids,
        missing_item_ids=missing_item_ids,
        assumptions=draft.assumptions,
        checks=checks,
        market_metrics=draft.market_metrics,
        business_concerns=draft.business_concerns,
    )


def criteria_inputs(dimensions: MarketDimensions) -> MarketCriteriaInputs:
    c2 = CriterionInput(
        score=dimensions.size_growth.score,
        score_status=dimensions.size_growth.score_status,
        evidence_ids=dimensions.size_growth.evidence_ids,
        rationale="C2는 비교 가능한 세부시장 CAGR 구간으로 계산",
    )
    demand, adoption = dimensions.demand, dimensions.commercialization
    if demand.score_status == adoption.score_status == ScoreStatus.SCORED:
        c3 = CriterionInput(
            score=round((demand.score + adoption.score) / 2, 2),
            score_status=ScoreStatus.SCORED,
            evidence_ids=sorted(set(demand.evidence_ids) | set(adoption.evidence_ids)),
            rationale="C3=(수요 점수+도입·상용화 점수)/2",
        )
    elif demand.score_status == adoption.score_status == ScoreStatus.NOT_APPLICABLE:
        c3 = CriterionInput(
            score_status=ScoreStatus.NOT_APPLICABLE, rationale="C3 비적용"
        )
    else:
        c3 = CriterionInput(
            score_status=ScoreStatus.UNKNOWN,
            evidence_ids=sorted(set(demand.evidence_ids) | set(adoption.evidence_ids)),
            rationale="수요 또는 도입·상용화 점수가 미확인이어서 C3 계산 불가",
        )
    c4 = CriterionInput(
        score=dimensions.monetization.score,
        score_status=dimensions.monetization.score_status,
        evidence_ids=dimensions.monetization.evidence_ids,
        rationale="C4는 5개 수익화 체크의 충족 개수",
    )
    return MarketCriteriaInputs(C2=c2, C3=c3, C4=c4)


def display_overall_score(dimensions: MarketDimensions) -> float | None:
    values = [
        result.score
        for result in (
            dimensions.size_growth,
            dimensions.demand,
            dimensions.commercialization,
            dimensions.monetization,
        )
        if result.score_status != ScoreStatus.NOT_APPLICABLE
    ]
    if not values or any(value is None for value in values):
        return None
    return round(sum(values) / len(values), 2)
