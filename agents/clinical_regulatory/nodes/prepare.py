from __future__ import annotations

try:
    from ..config import CATEGORY_TO_STEPS, KEYWORD_TO_STEPS, STEP_ORDER
    from ..context import ClinicalAgentState
except ImportError:
    from config import CATEGORY_TO_STEPS, KEYWORD_TO_STEPS, STEP_ORDER
    from context import ClinicalAgentState


def prepare_node(state: ClinicalAgentState) -> dict:
    """실행 단계와 대상 제품 결정 (코드로 고정)"""
    ctx = state["ctx"]
    all_pids = [p.product_id for p in ctx.company.products]
    if ctx.mode == "initial" or not ctx.previous:
        ctx.steps, ctx.target_pids = list(STEP_ORDER), all_pids
        return {}
    if not ctx.requests:  # 모든 요청이 라운드 한도 초과 → 조회 없이 응답만
        ctx.steps, ctx.target_pids = [], []
        return {}

    cat_by_fid = {f["finding_id"]: f["category"] for f in ctx.prev_findings}
    wanted: set[str] = set()
    pids: set[str] = set()
    for r in ctx.requests:
        pids |= set(r.product_ids)
        for fid in r.finding_ids:
            wanted |= set(CATEGORY_TO_STEPS.get(cat_by_fid.get(fid, ""), []))
        if r.criterion_id == "C1":
            wanted |= {"studies", "claims"}
        text = " ".join(q.text for q in r.questions)
        for words, steps in KEYWORD_TO_STEPS:
            if any(w in text for w in words):
                wanted |= set(steps)
    if not wanted:
        wanted = set(STEP_ORDER)
    if "classify" in wanted:                  # 제품 판단이 바뀌면 후속 단계 전체 갱신
        wanted = set(STEP_ORDER)
    if wanted & {"regulatory", "studies"}:    # 주장 대조는 종속 갱신
        wanted.add("claims")
    ctx.steps = [s for s in STEP_ORDER if s in wanted]
    ctx.target_pids = [p for p in all_pids if not pids or p in pids]
    # 새로 추가된 제품은 유형 분류가 필요
    if any(p not in ctx.classify for p in ctx.target_pids) and "classify" not in ctx.steps:
        ctx.steps.insert(0, "classify")
    return {}
