# Risk Agent — 고정 데이터 기반 LangGraph

공개 자료에서 외부 자원 의존, 핵심 역할 연속성, 운영 관련 사건을 정리합니다. 자료 부족은 위험이 없다는 의미가 아닙니다. 인용 연결 검사도 사실성 검증을 대신하지 않습니다.

## 흐름

```text
START → load_dataset → select_evidence → analyze_risk → review_result
                            ↑                             │
                            └── 보완 가능·새 자료 있음 ───┤
                                                          └→ export_result → END
```

[LangGraph StateGraph](https://docs.langchain.com/oss/python/langgraph/graph-api)로 구현했습니다. 외부 검색은 없습니다. 미확인 사항·검증 오류·실사 질문이 남으면 규칙 기반 검토 노드가 아직 사용하지 않은 자료를 선택해 최대 1회 보완합니다. 기본 모델 요청은 실행당 최대 2회이며, 캐시가 있거나 근거 구간이 없으면 호출하지 않습니다. 실패 결과도 캐시하므로 실패는 `analysis_status`로 확인해야 합니다.

## 파일

| 파일 | 역할 |
|---|---|
| `agent.py` | State 정의, 다섯 노드, 조건부 루프, 상위 그래프용 RiskAgent |
| `dataset.py` | manifest 기준 데이터 로딩·해시 검사 |
| `company_analysis.py` | 기존 GPT 구조화 응답·근거 검사·캐시 로직 |
| `__main__.py` | CLI |
| `collection/` | 별도 데이터 수집·선별 도구. 기본 Agent에서 호출하지 않음 |
| `legacy_agent.py`, `schema.py`, `providers.py`, `prompt.md` | 이전 검색·재검토 구현과 계약. 기본 Agent에서 사용하지 않음 |
| `data/` | 고정 manifest, 원본 데이터, 모델 캐시 (Git 제외) |

## 데이터 고정

`full_20260930_032104/*/reviewed/state.json`의 42개 기업을 `data/frozen_manifest.json`에 등록했습니다. Agent는 등록된 파일을 읽고 SHA-256을 검사하며 원본을 수정하지 않습니다. 새 수집 실행은 이 목록에 자동 반영되지 않습니다. 고정은 입력 버전 관리이며 자료가 사실 검증되었다는 뜻은 아닙니다.

팀원에게는 manifest와 그 안의 상대 경로에 해당하는 파일을 함께 전달해야 합니다. Git만 clone하면 기업 데이터는 없습니다. manifest와 원본을 모두 바꾸는 행위까지 막는 파일시스템 잠금은 아닙니다.

## 실행

프로젝트 루트에서 의존성을 설치하고 `.env`에 `OPENAI_API_KEY`, `RISK_MODEL`을 설정합니다.

```bash
python -m pip install -r requirements.txt
python -m agents.risk --company-id <company_id> --output outputs/risk_result.json
```

기업 ID는 manifest의 `companies` 키에서 확인합니다. `--manifest`로 다른 고정 데이터 목록을 지정할 수 있습니다. 기존 CLI의 `--input`, `--demo`, `--max-search-calls`, `--max-reviews`는 제거했습니다.

## 상위 LangGraph 연결

```python
from agents.risk import RiskAgent

builder.add_node("risk", RiskAgent())
# 상위 State의 company_profile에는 manifest에 등록된 company_id가 있어야 합니다.
# 병렬 노드들이 references에 기록하면 상위 State에 중복 ID를 병합하는 reducer를 둡니다.
```

독립 실행:

```python
from agents.risk import build_risk_graph

result = build_risk_graph().invoke({
    "company_profile": {"company_id": "manifest에 등록된 ID"}
})
```

공통 State에 돌려주는 dict:

```python
{
    "risk_analysis": {
        "schema_version": "risk-company-3",
        "company_id": "...",
        "company_name": "...",
        "as_of": "...",
        "dataset_sha256": "...",
        "analysis_status": "complete | partial | failed | no_evidence",
        "areas": [
            # category, observations, unknowns, due_diligence_questions
            # observations에는 statement, kind, conditional_impact, citations 등이 포함됨
        ],
        "diagnostics": [],
        "passages": [],
        "mode": "api | cache | no_evidence | blocked",
        "cache_key": "...",
        "usage": None,
        "limitation": "..."
    },
    "references": []  # 분석 입력 구간에 포함된 원문 출처
}
```

이 형식은 이전 `RiskAnalysis` Pydantic 모델의 `findings/coverage` 형식과 다릅니다. 다음 판단 Agent는 `risk_analysis.areas`를 읽어야 합니다. `review_requests["risk"]`에 company_id, attempt=1, questions를 넣고 같은 기업의 이전 risk_analysis를 전달하면 질문 기반 재검토도 가능합니다. 외부 호출의 반복 횟수는 상위 그래프가 관리해야 합니다. `complete`는 실행·형식 처리 상태이고 저위험 판정이 아닙니다.

## API 없는 테스트

```bash
python -m unittest agents.risk.test_graph agents.risk.test_company_analysis agents.risk.test_output_contract agents.risk.collection.test_collect agents.risk.collection.test_quality
python -m unittest discover -s tests
```

가상 모델 응답으로 실제 그래프, 상위 그래프 연결, 해시 변경 차단, 무근거 입력, 실패 상태, 캐시 재사용을 검증합니다. 실제 데이터 재수집이나 유료 모델 호출은 하지 않습니다.


## 보완 루프와 비용

- 기본 최초 선택은 최대 8개 원문이며, 보완 시 최대 8개를 추가합니다. 질문 단어와 본문의 일치 수로 자료 순위를 정합니다. 정교한 의미 검색은 아닙니다.
- `review_result`는 미확인 사항·실사 질문·검증 진단을 검사하는 규칙 기반 노드입니다. 사실성이나 판단 비약을 완전히 탐지하는 별도 LLM 심사관은 아닙니다.
- 보완 프롬프트에는 이전 영역별 결과와 질문이 들어갑니다. 새 원문 없이 동일 요청을 반복하지 않습니다.
- 종료 사유: `review_passed`, `no_new_evidence`, `review_limit`, `no_evidence`, `analysis_failed`.
- `risk_analysis.review_history`에서 매 회차 질문·출처·분기·캐시 여부를 확인합니다. 보완 실패 시 1차 결과를 유지하되 partial과 오류를 기록합니다.
- 최대 40개 원문 구간을 모델에 전달하므로 전체 고정 자료를 완전히 검토했다고 해석하면 안 됩니다.
- `RiskAgent(max_reviews=0)`으로 내부 보완을 끌 수 있습니다. 외부 심사 루프에서도 상위 반복 한도를 반드시 설정하세요.
- 입력 구간이나 프롬프트가 바뀌어 기존 캐시가 맞지 않으면 새 API 비용이 발생합니다. 이번 구현 검증은 가상 응답으로만 수행했습니다.

외부 재검토 입력 예시:

```python
state = {
    "company_profile": {"company_id": "고정 데이터 ID"},
    "risk_analysis": previous_result,
    "review_requests": {"risk": {
        "company_id": "고정 데이터 ID",
        "attempt": 1,
        "reason": "협약 사실만으로 의존성을 판단했는지 재검토",
        "questions": ["다른 협력 기관이나 대체 자원 관련 근거가 있는가?"]
    }}
}
result = RiskAgent()(state)
```
