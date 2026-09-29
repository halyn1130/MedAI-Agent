# 현재 실행 경로 안내

기본 CLI는 기업당 1회 호출·캐시 방식으로 변경되었습니다. [v3 실행·출력 계약](PIPELINE_V3.md)을 먼저 참고하세요. 아래 영역별 노드 설명은 이전 계약입니다.

# 리스크 분석 Agent

외부 자원 의존, 핵심 인력·역할의 연속성, 운영 관련 사건만 분석합니다.
RAG·투자 점수 계산·최종 투자 판단은 하지 않습니다.
자료가 없는 항목은 위험을 생성하지 않고 coverage에 남깁니다.

## 실행 흐름도

![리스크 Agent 실행 흐름](../../docs/diagrams/risk_workflow.png)

[SVG 확대 보기](../../docs/diagrams/risk_workflow.svg)

현재 구현 기준이며, 외부 그래프의 6번 라우팅은 연동 지점으로 표시했습니다.

## 실행

Python 3.10 이상에서 프로젝트 requirements.txt를 설치합니다.

```bash
python -m pip install -r requirements.txt
python -m agents.risk --input examples/risk/input.json --output outputs/risk_demo.json --demo
```

데모는 네트워크나 LLM을 호출하지 않는 연결 확인용입니다. 실제 분석 결과가 아닙니다.
실제 실행에는 `.env`에 `OPENAI_API_KEY`, `RISK_MODEL`, `TAVILY_API_KEY`를 설정합니다.
RISK_MODEL에는 사용 계정에서 접근 가능하고 tool calling을 지원하는 모델 ID를 넣습니다.
실제 실행은 검색 질의를 Tavily에, 기업 프로필과 증거 텍스트를 모델 서비스에 전송합니다.

```bash
python -m agents.risk --input company_state.json --output outputs/risk_result.json
```

입력은 공유 State 형태의 JSON입니다. CSV는 1번 데이터 입력·정규화 단계가 한 기업의
`company_profile`로 변환해 전달합니다. 1번은 제공 데이터셋만 정리하며 추가 수집을 하지 않습니다. Risk는 기존 자체 검색으로 운영 관련 자료를 보완합니다. CSV 자동 조사·기업 적격성 판단은 이 Agent의 범위가 아닙니다.
`company_profile`의 company_id와 company_name은 필수이며 제품·협력·경영진·공지 정보는 자유로운 dict로 받습니다.
근거는 `company_evidence`에 Source 스키마로 전달합니다. URL만 주면 원문을 직접 읽은 것으로 처리하지 않습니다.
Source의 content에는 확보한 발췌문/본문을 넣어야 합니다. sources에도 검증용 content가 보존됩니다.
입력 내용은 실제 모델에 전달되므로 공개 자료만 사용하세요.

## State 연동

```python
from typing import Annotated, TypedDict
from agents.risk import RiskAgent, merge_by_agent, merge_references
from agents.risk.providers import ChatAnalyzer, TavilySearch

class State(TypedDict, total=False):
    company_profile: dict
    company_evidence: list[dict]
    as_of: str
    risk_analysis: dict
    review_requests: dict
    review_results: Annotated[dict, merge_by_agent]
    retry_counts: Annotated[dict, merge_by_agent]
    references: Annotated[list[dict], merge_references]
    risk_review_history: list[dict]

risk_node = RiskAgent(TavilySearch(), ChatAnalyzer())
# builder.add_node('risk', risk_node)
delta = risk_node(state)  # 전체 State를 덮어쓰지 않는 노드 업데이트
```

최초 실행은 `review_requests['risk']`를 생략하거나 null로 둡니다.
보완 실행은 기존 risk_analysis와 `examples/risk/review_request.json` 형태의 요청을 전달합니다.
attempt는 retry_counts['risk'] + 1이어야 하며 서버 쪽 max_reviews보다 클 수 없습니다.
한도는 요청이 임의로 높일 수 없으며 동일 request_id의 재처리도 차단합니다.
실패한 보완도 시도 횟수에 포함됩니다.

finding_ids가 있으면 지정 항목만 동일 ID로 갱신합니다. 없으면 categories에 지정된
영역 전체를 다시 검토할 수 있습니다. 요청 밖의 항목은 그대로 유지됩니다.
보완 결과는 review_results['risk']와 risk_review_history로 반환합니다.
그래프 담당자는 출력의 delta를 적용하고 요청을 소비한 뒤 6번으로 연결하세요.
새 회사로 이동할 때 분석·요청·횟수·보완 이력을 명시적으로 초기화해야 합니다.
병합 Reducer에서는 빈 dict 업데이트만으로 이전 키가 지워지지 않습니다.
전체 그래프와 다른 Agent는 이 구현에 포함하지 않습니다.

## 제약과 검증

- 검색 예산은 호출 단위로 강제하며, 성공한 동일 질의는 반복하지 않습니다.
- 보완 요청 질문과 기간이 검색 입력에 반영됩니다. preferred_sources는 분석 지침이며 도메인 강제 필터가 아닙니다.
- 출처 ID·영역·보완 대상·질문 ID·coverage 정합성을 검증합니다.
- 검색 결과의 source_type은 확정하지 않고 other로 보존합니다. 도메인은 publisher의 보수적 대체값입니다.
- 발행일을 확보하지 못한 출처는 날짜 미상으로 취급합니다. 날짜 미상 자료로 과거 상태를 확정하면 안 됩니다.
- URL과 본문이 같은 자료는 중복 제거합니다. 다른 URL의 재전재·동명이인·의미상 근거 일치는 LLM 검토와 사람 확인이 필요합니다.
- 신규 수집 문서는 반드시 관련성 있는 독립 근거라는 뜻은 아닙니다. coverage와 citations를 함께 확인하세요.
- 검증 실패 시 기존 항목을 보존하고 실패·제한을 기록합니다. 최초 실패에는 가짜 결과를 채우지 않습니다.
- 자동 검증은 구조 검증이며 사실 검증을 대체하지 않습니다.

```bash
python -m unittest discover -s tests -v
```

연동 참고: [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search),
[LangChain structured output](https://reference.langchain.com/python/langchain-openai/chat_models/base/BaseChatOpenAI/with_structured_output).

## 출력 검증 보완

실제 모델 호출은 strict JSON Schema로 구조화하고 Pydantic으로 다시 검증합니다.
`facts[].citations`에는 `source_id`와 원문 그대로의 `quote`가 필요합니다.
인용 누락·원문에 없는 인용은 `evidence_validation` 실패로 기록합니다.
이는 인용 문자열 검증이며, 주장의 의미와 인용의 일치까지 보장하지는 않습니다.

사건 근거가 없으면 `findings=[]`로 반환하고 `coverage[].unknowns`와
`coverage[].due_diligence_questions`에 미확인 사항·실사 질문을 남깁니다.
`--max-search-calls 0`은 제공 자료 전용 모드이며 Tavily 키가 필요하지 않습니다.
이 경우 검색 로그의 `skip_reason=disabled`이며 실행 완료와 자료 충분 여부를 구분합니다.
`analysis_status=complete`여도 사실이 모두 확인됐다는 의미는 아닙니다.

실패 상세는 `risk_analysis.diagnostics`에서 확인합니다. 전체 실패 시 CLI 종료 코드는 1입니다.
기존 결과 파일은 자동 변경되지 않으며 새 실행이 필요합니다.

출력 형식·인용 검증 실패 시 해당 영역만 최대 1회 수정 요청합니다.
오류 사유는 `missing_source_quotation`, `unknown_source_id`, `empty_quotation`,
`quotation_not_in_source`로 구분하며 `attempts`에 시도 횟수를 기록합니다.
인용 문자열 기준은 완화하지 않습니다. 실패가 지속되면 분석 실패로 남깁니다.
정상적으로 처리된 다른 영역은 재호출하지 않습니다. 3개 영역 기준 모델 분석은
최대 6회이며, 별도의 SDK 통신 재시도(max_retries=1)는 추가될 수 있습니다.
자동 수정 요청에는 기존 응답과 안전한 검증 피드백이 포함되며 로그에 원문·키를 저장하지 않습니다.
