"""보고서 품질 평가 (Hybrid = 규칙 검사 + LLM Judge). 보고서를 고치지 않고 판정과 반려 사유만 만든다.

기준
- groundedness : 주장이 출처로 추적되는가. 입력에 없는 숫자(check_narrative 반려), 출처 없는 사실, Judge 판정
- neutrality   : 주관적 추천·우열 표현이 없는가. 정책 점수에 따른 순위·총점·선정은 허용하고, 그 밖의 평가어·권유는 금지
- bias         : 단일 출처·유리한 근거 편중이 없는가. 기업별 출처 수·발행처 수·회사 자체 발표 비중, 확인 필요 사항 서술
- coverage     : 선정 기업마다 4개 관점(시장·임상·실적·운영 리스크)이 있는가
- format       : 쪽수·인용 번호·후보 누락 (checker.check_report)

결과는 공통 State의 QualityIssue(common_state.md 6번)로 바꿔 반환한다 (to_quality_issues). 경로는 Orchestrator가 정한다.
- report         : LLM 서술(narrative) 문제. description이 '항목 키: [기준] 사유'라 작성 노드가 그 항목만 다시 쓴다.
- missing_data   : 선정 기업의 출처 편중·출처 미연결 (data_limitations). 보고서 "4. 보완 이력·한계"에도 공개한다.
- interpretation : Judge가 지적한 코드 본문(Worker 결과) 내용 문제
- execution      : 쪽수·인용 번호·후보 누락·관점 섹션 누락 등 코드가 만든 형식 오류
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .checker import _section, check_report

CRITERIA = ("groundedness", "neutrality", "bias", "coverage", "format")
CRITERION_KO = {"groundedness": "근거성", "neutrality": "중립성", "bias": "편향 통제", "coverage": "관점 커버리지",
                "format": "형식"}
VIEW_KO = {"market": "시장성", "clinical": "기술 성숙도(임상·인허가)", "traction": "도메인 적용(실적)",
           "risk": "이해관계자(운영 리스크)"}
VIEW_HEADINGS = ("#### 고객 문제·시장", "#### 기술·임상·인허가", "#### 사업모델·실적·성장", "#### 운영 리스크")
SUMMARY_VIEWS = (("임상",), ("시장",), ("실적",), ("운영", "리스크"))

# 중립성: 점수에 근거하지 않은 평가어·권유. 순위·총점 표현("1위", "총점 67점")은 허용한다.
SUBJECTIVE = ("투자해야", "투자를 권", "투자 매력", "매력적", "추천", "유망", "경쟁력 있", "경쟁력이 있", "우수", "뛰어",
              "탁월", "선도", "혁신적", "신뢰할 수 있", "압도적", "독보적", "최고의", "우월", "보다 낫")
# 편향 통제: 선정 기업 논점에 확인 필요 사항(0점 처리·경고·실사)이 들어갔는지
CAVEAT = ("확인", "0점", "실사", "필요", "못했", "경고", "부족", "한계", "불확실")

MIN_SOURCES = 2              # 선정 기업당 인용 출처 수
MIN_PUBLISHERS = 2           # 선정 기업당 서로 다른 발행처 수
MAX_SELF_RATIO = 0.5         # 회사 자체 발표 비중 상한
SELF_TYPES = {"company", "press_release", "company_website"}
JUDGE_PASS = 4               # Judge 점수(1~5) 통과 기준
MAX_JUDGE_CHARS = 12000      # Judge에 넘기는 본문 길이
DEFAULT_JUDGE_MODEL = "gpt-4o"   # 작성 모델(gpt-4.1)과 다른 모델로 자기 평가 편향을 줄인다
JUDGE_PROMPT = (Path(__file__).parent / "prompts/judge.md").read_text(encoding="utf-8")


class Issue(BaseModel):
    criterion: str
    target: str                  # narrative 키(summary, select:<id>, points:<id> …) 또는 report
    message: str
    fixable: bool
    source: str = "rule"         # rule / judge


# ── 데이터 한계 (취합 단계에서 계산, 보고서 한계 섹션에 공개) ─────────────────────
def _company_refs(c: dict) -> dict[str, list[list[int]]]:
    """관점별 사실 항목의 출처 번호 목록."""
    cl, tr = c["clinical"], c["traction"]
    return {"market": [m["refs"] for m in c["market"]["metrics"]],
            "clinical": [x["refs"] for x in cl["regulatory"] + cl["studies"]],
            "traction": [x["refs"] for x in tr["revenue"] + tr["contracts"]],
            "risk": [o["refs"] for a in c["risk"]["areas"] for o in a["observations"]]}


def _publisher(ref: dict) -> str:
    return (ref.get("publisher") or urlparse(ref.get("url") or "").netloc.removeprefix("www.") or "미상").strip().lower()


def _is_self(ref: dict, name: str) -> bool:
    return ref.get("source_type") in SELF_TYPES or bool(name and name in (ref.get("publisher") or ""))


def data_limitations(ctx: dict) -> list[dict]:
    """선정 기업별 출처 편중·출처 미연결. 보고서를 다시 써도 바뀌지 않는 데이터 한계라 Loop 대상이 아니다."""
    out = []
    for c in ctx["selected"]:
        facts = _company_refs(c)
        nums = sorted({n for refs in facts.values() for r in refs for n in r})
        items = [ctx["references"][n - 1] for n in nums if 0 < n <= len(ctx["references"])]
        bias = []
        if len(nums) < MIN_SOURCES:
            bias.append(f"인용 출처 {len(nums)}건")
        elif len({_publisher(r) for r in items}) < MIN_PUBLISHERS:
            bias.append(f"출처 {len(nums)}건이 발행처 1곳에 집중")
        if items and (ratio := sum(_is_self(r, c["name"]) for r in items) / len(items)) > MAX_SELF_RATIO:
            bias.append(f"회사 자체 발표 비중 {ratio:.0%}")
        if bias:
            out.append({"company_id": c["company_id"], "criterion": "bias", "text": f"{c['name']}: {' · '.join(bias)}",
                        "workers": [v for v, refs in facts.items() if any(refs)] or list(facts)})
        grounded, workers = [], []
        if empty := [v for v, refs in facts.items() if not any(refs)]:
            grounded.append(f"출처가 연결된 사실이 없는 관점 {', '.join(VIEW_KO[v] for v in empty)}")
            workers += empty
        uncited = {v: sum(1 for r in refs if not r) for v, refs in facts.items()}
        if n := sum(uncited.values()):
            grounded.append(f"출처가 연결되지 않은 사실 {n}건")
            workers += [v for v, k in uncited.items() if k and v not in workers]
        if grounded:
            out.append({"company_id": c["company_id"], "criterion": "groundedness",
                        "text": f"{c['name']}: {' · '.join(grounded)}", "workers": workers})
    return out


# ── 규칙 검사 ────────────────────────────────────────────────────────────────
def note_key(note: str) -> str:
    """'points:c001: 사유' → 'points:c001', 'summary: 사유' → 'summary'."""
    kind, _, rest = note.partition(":")
    return f"{kind}:{rest.split(':')[0]}" if kind in ("select", "points", "reason", "ref") else kind


def _writer_issues(notes: list[str]) -> list[Issue]:
    """작성 노드의 check_narrative 반려 → 다시 쓰면 고칠 수 있는 문제."""
    out = []
    for n in notes:
        if n.startswith("LLM 서술 없음") or "보고서 대상이 아닌" in n:
            continue
        criterion = "groundedness" if "숫자" in n else "format"
        out.append(Issue(criterion=criterion, target=note_key(n), message=n.split(": ", 1)[-1], fixable=True))
    return out


def _company_section(markdown: str, c: dict) -> str:
    head = f"### {c['rank']}. {c['name']}"
    start = markdown.find(head)
    if start < 0:
        return ""
    ends = [i for i in (markdown.find("\n### ", start + 1), markdown.find("\n## ", start + 1)) if i > 0]
    return markdown[start:min(ends) if ends else None]


def rule_issues(markdown: str, ctx: dict, narrative: dict, writer_notes: list[str]) -> tuple[list[Issue], dict]:
    fmt = check_report(markdown, ctx)
    issues = [Issue(criterion="format", target="report", message=i, fixable=False) for i in fmt["issues"]]
    issues += _writer_issues(writer_notes)

    for key, text in narrative.items():
        if words := [w for w in SUBJECTIVE if w in (text or "")]:
            issues.append(Issue(criterion="neutrality", target=key, fixable=True,
                                message=f"점수에 근거하지 않은 평가·권유 표현 {words} → 사실·점수로만 서술"))
        if key.startswith("points:") and not any(w in (text or "") for w in CAVEAT):
            issues.append(Issue(criterion="bias", target=key, fixable=True,
                                message="강점만 있고 확인이 필요한 점(0점 처리 항목·경고·실사 질문)이 없음"))

    limits = "## 4. 보완 이력·한계"
    section4 = markdown[markdown.find(limits):] if limits in markdown else ""
    for lim in ctx.get("limitations") or []:
        if lim["text"] not in section4:
            issues.append(Issue(criterion=lim["criterion"], target="report", fixable=False,
                                message=f"데이터 한계가 보고서에 공개되지 않음: {lim['text']}"))

    for c in ctx["selected"]:
        body = _company_section(markdown, c)
        if missing := [h.removeprefix("#### ") for h in VIEW_HEADINGS if h not in body]:
            issues.append(Issue(criterion="coverage", target="report", fixable=False,
                                message=f"{c['name']} 관점 누락: {', '.join(missing)}"))
    if summary := narrative.get("summary"):
        if missing := [v[0] for v in SUMMARY_VIEWS if not any(w in summary for w in v)]:
            issues.append(Issue(criterion="coverage", target="summary", fixable=True,
                                message=f"요약에 분석 분야 누락: {', '.join(missing)} → 임상·시장·실적·운영 4개 분야를 모두 언급"))
    return issues, fmt


# ── LLM Judge ───────────────────────────────────────────────────────────────
class JudgeIssue(BaseModel):
    criterion: str = Field(description="groundedness | neutrality | bias")
    target: str = Field(description="문제 문장이 있는 narrative 키 그대로, 코드가 만든 본문이면 report")
    problem: str = Field(description="무엇을 어떻게 고쳐야 하는지 한 문장")


class JudgeVerdict(BaseModel):
    groundedness: int = Field(description="1~5")
    neutrality: int = Field(description="1~5")
    bias: int = Field(description="1~5")
    issues: list[JudgeIssue] = Field(default_factory=list)


def get_judge() -> Optional[Any]:
    model = os.environ.get("REPORT_JUDGE_MODEL")
    if not model and os.environ.get("OPENAI_API_KEY"):
        model = DEFAULT_JUDGE_MODEL
    if not model:
        return None
    from langchain.chat_models import init_chat_model
    return init_chat_model(model if ":" in model else f"openai:{model}", temperature=0)


def judge_input(markdown: str, ctx: dict, narrative: dict) -> dict:
    from .writer import EASY, llm_input
    facts = llm_input(ctx)
    return {"facts": {k: facts[k] for k in ("as_of", "candidates", "status", "k", "selected_list", "selected",
                                            "reason_counts", "zero_filled_top")},
            "item_names": EASY, "narrative": narrative,
            "report_selected_section": _section(markdown, "2. 선정 기업")[:MAX_JUDGE_CHARS]}


def judge_report(judge: Any, markdown: str, ctx: dict, narrative: dict) -> JudgeVerdict:
    messages = [("system", JUDGE_PROMPT), ("user", json.dumps(judge_input(markdown, ctx, narrative), ensure_ascii=False))]
    return judge.with_structured_output(JudgeVerdict).invoke(messages)


# ── 종합 ────────────────────────────────────────────────────────────────────
def evaluate(markdown: str, ctx: dict, narrative: dict, writer_notes: list[str], judge: Any = None) -> dict:
    """{"passed", "criteria": {기준: {passed, judge_score, issues}}, "feedback", "warnings", "format", "judge"}.
    feedback = 작성 노드가 고칠 수 있는 반려 사유('키: [기준] 사유'). 비어 있으면 다시 써도 소용없다."""
    issues, fmt = rule_issues(markdown, ctx, narrative, writer_notes)
    scores: dict[str, Optional[int]] = {}
    judge_state = "skipped"
    if judge is not None:
        try:
            v = judge_report(judge, markdown, ctx, narrative)
            judge_state = "ok"
            for name in ("groundedness", "neutrality", "bias"):
                scores[name] = max(1, min(5, int(getattr(v, name))))
            for i in v.issues:
                name = i.criterion if i.criterion in scores else "groundedness"
                if scores[name] >= JUDGE_PASS:
                    continue  # 통과한 기준의 사소한 지적은 반려 사유로 쓰지 않는다
                fixable = i.target in narrative
                issues.append(Issue(criterion=name, target=i.target if fixable else "report", message=i.problem,
                                    fixable=fixable, source="judge"))
            for name, score in scores.items():
                if score < JUDGE_PASS and not any(i.criterion == name and i.source == "judge" for i in issues):
                    issues.append(Issue(criterion=name, target="report", source="judge", fixable=False,
                                        message=f"Judge 점수 {score}점 (사유 없음)"))
        except Exception as e:  # noqa: BLE001 - Judge 실패 시 규칙 검사 결과로만 판정한다
            judge_state = f"failed: {type(e).__name__}: {e}"[:200]

    criteria = {}
    for name in CRITERIA:
        found = [i for i in issues if i.criterion == name]
        criteria[name] = {"passed": not found, "judge_score": scores.get(name),
                          "issues": [i.model_dump() for i in found]}
    feedback = list(dict.fromkeys(f"{i.target}: [{CRITERION_KO[i.criterion]}] {i.message}" for i in issues if i.fixable))
    warnings = [f"[{CRITERION_KO[lim['criterion']]}] {lim['text']}" for lim in ctx.get("limitations") or []]
    return {"passed": not issues, "criteria": criteria, "feedback": feedback, "warnings": warnings,
            "format": fmt, "judge": judge_state}


# ── 공통 State QualityIssue로 변환 ───────────────────────────────────────────
QUESTION = {"bias": "{name}의 판단을 뒷받침할 다른 발행처의 독립 출처를 확보할 수 있는가?",
            "groundedness": "{name}의 해당 관점 사실을 뒷받침할 출처를 확보할 수 있는가?"}


def to_quality_issues(verdict: dict, ctx: dict) -> list[dict]:
    """evaluate 결과 + 데이터 한계 → QualityIssue 목록 (issue_id, kind, target_workers, description,
    finding_ids, missing_ids, questions)."""
    out: list[dict] = []

    def add(kind: str, description: str, workers=(), questions=()):
        out.append({"issue_id": f"qi{len(out) + 1:02d}", "kind": kind, "target_workers": list(workers),
                    "description": description, "finding_ids": [], "missing_ids": [], "questions": list(questions)})

    for name, c in verdict["criteria"].items():
        for i in c["issues"]:
            if i["fixable"]:
                add("report", f"{i['target']}: [{CRITERION_KO[name]}] {i['message']}")
            elif i["source"] == "judge":
                add("interpretation", f"[{CRITERION_KO[name]}] {i['message']}")
            else:
                add("execution", f"[{CRITERION_KO[name]}] {i['message']}")
    names = {c["company_id"]: c["name"] for c in ctx["selected"]}
    for lim in ctx.get("limitations") or []:
        add("missing_data", f"[{CRITERION_KO[lim['criterion']]}] {lim['text']}", lim.get("workers") or [],
            [QUESTION[lim["criterion"]].format(name=names.get(lim["company_id"], lim["company_id"]))])
    return out


def quality_status(issues: list[dict]) -> str:
    """pass | needs_revision | failed (실행 오류가 있으면 failed)."""
    if any(i["kind"] == "execution" for i in issues):
        return "failed"
    return "needs_revision" if issues else "pass"
