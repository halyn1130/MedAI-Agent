# 시장·사업성 분석 Agent

## 구현 범위

시장 Agent는 기업·제품 Agent의 `company_profile`을 입력으로 받아 다음 순서로 실행됩니다.

```text
입력 정보 점검
  └─ 필요한 경우에만 기업별 Web Search
시장 범위 정의
4개 영역별 RAG 질의 생성
  ├─ 시장 규모·성장성
  ├─ 고객·시장 수요
  ├─ 도입·상용화 가능성
  └─ 수익화 가능성
결과 병합 → market_analysis
```

네 분석 노드는 LangGraph fan-out/fan-in으로 실행됩니다. 각 노드는 자기 결과 키(`size_growth_result`, `demand_result`, `commercialization_result`, `monetization_result`)에만 쓰고, 공통 목록은 ID 기반 reducer로 합칩니다.

## Web Search와 RAG의 경계

- Web Search(Tavily): 제품 목적, 목표 고객, 구매자, 사용자, 사업모델, 진출 지역처럼 기업마다 달라지는 사실을 보완합니다. `company_profile`이 충분하면 호출하지 않습니다. 검색 결과의 AI 답변은 쓰지 않고, 원문 URL과 원문 내용이 있는 결과만 Source/Evidence 후보로 사용합니다.
- RAG: 시장 규모·CAGR, 고객군의 수요, 도입 조건, 조달 구조, 일반적인 수익모델처럼 산업 공통 근거를 검색합니다.

검색 실패나 자료 미발견은 부정 사실이 아닙니다. 점수를 만들 근거가 부족하면 `score=null`, `score_status=unknown`으로 남깁니다.

## 데이터셋 설치

`market_rag_dataset.zip`을 압축 해제한 후 프로젝트 루트의 `data/market_rag/` 디렉토리에 넣어주세요.

최종 구조는 아래와 같습니다.

```text
MedAI-Agent/
└── data/
    └── market_rag/
        ├── reports/
        │   └── *.pdf
        ├── papers/
        │   └── *.pdf
        └── metadata.csv
```

PDF와 `metadata.csv`는 Git에 올리지 않습니다. `metadata.example.csv`만 형식 예제로 추적합니다. 현재 로컬 데이터셋은 원본 202쪽이며, 빈 페이지 2쪽을 `metadata.csv`의 `exclude_pages`로 제외해 인덱싱 예산을 정확히 200쪽으로 맞춥니다.

`metadata.csv` 최소 필드는 다음과 같습니다.

| 필드 | 설명 |
|---|---|
| `path` | `data/market_rag/` 기준 PDF 상대 경로 |
| `document_id` | 안정적인 문서 식별자 |
| `title`, `publisher`, `published_at` | 출처 표시용 메타데이터 |
| `region`, `segment`, `source_type` | 검색 결과의 적용 범위 |
| `source_url` | 원문 또는 공식 배포 페이지 |
| `include_pages`, `exclude_pages` | 1부터 시작하는 페이지 범위. 예: `1-10,15` |

페이지 예산 확인:

```bash
python -m rag.market.dataset_stats
```

## Vector Index 생성

기본 임베딩은 `BAAI/bge-m3`, 저장소는 로컬 persistent Chroma입니다. 청크 크기는 약 700 whitespace tokens, overlap은 100이며 환경변수로 바꿀 수 있습니다.

```bash
python -m rag.market.build_index --rebuild
```

생성 위치는 `data/vectorstore/market/`이며 Git에서 제외됩니다. 원본 데이터셋과 위 명령으로 언제든 재생성할 수 있습니다. 현재 구현은 dense retrieval baseline이며, retriever에 optional reranker hook만 열어 두었습니다. BM25 hybrid는 아직 구현하지 않았습니다.

## 실행

환경변수는 `.env.example`을 복사해 설정합니다. 라이브 분석에는 `OPENAI_API_KEY`, `MARKET_MODEL`, `TAVILY_API_KEY`가 필요합니다.

```bash
python -m agents.market \
  --input examples/market/input.json \
  --output outputs/market_result.json
```

API 없이 그래프와 출력 계약만 확인하려면 다음을 실행합니다. 데모는 사실이나 점수를 생성하지 않고 `unknown`을 반환합니다.

```bash
python -m agents.market \
  --input examples/market/input.json \
  --output outputs/market_demo.json \
  --demo
```

상위 LangGraph에서는 `agents.market.market_agent_node` 또는 `agents.market.market_node`를 노드로 사용할 수 있습니다. 입력 State의 `company_profile`, `as_of`, `run_id`, `market_analysis`, `review_requests`를 읽고 `market_analysis`만 갱신합니다.

## 입력과 출력

필수 입력은 `company_profile.company_id`와 회사명입니다. 시장 범위에 필요한 나머지 필드는 누락될 수 있으며 그때만 Web Search로 보완합니다.

CLI 출력은 아래처럼 `market_analysis` envelope와 선택적인 단일 보완 응답을 감싸는 구조입니다.

```text
market_analysis: { ...아래 envelope... }
review_response: null | { ...현재 실행에서 생성된 보완 응답... }
```

상위 LangGraph의 `market_agent_node`는 공통 State에 맞춰 `market_analysis` 키만 갱신합니다. `market_analysis` envelope의 주요 필드는 다음과 같습니다.

```text
agent, company_id, as_of, schema_version, criteria_version
analysis_status, result_version
data.market_scope
data.supplemental_context
data.market_metrics
data.dimensions.{size_growth,demand,commercialization,monetization}
data.criteria_inputs.{C2,C3,C4}
data.overall_market_score  # 표시용, 최종 투자점수에 중복 가산 금지
sources, evidence, findings, missing_items
retrieval_log, web_search_log, review_responses
```

시장 지표에는 값뿐 아니라 통화, 단위, 지역, 세그먼트, 기준연도·전망기간, 산출 방법, 가정, 근거 ID를 보존합니다. 최종 투자 가중치와 최종 판정은 이 Agent가 계산하지 않습니다.

## Review

`ReviewRequest`의 `dimensions` 또는 `criterion_id`로 지정한 영역만 다시 실행합니다. 기업별 사실 보완은 Web Search, 시장·산업 근거 보완은 RAG로 라우팅합니다. `review_round=1`까지만 처리하고 같은 요청을 중복 처리하지 않습니다. 해결되지 않은 항목은 0점으로 바꾸지 않고 `unknown`으로 유지합니다.

## 테스트

```bash
python -m pytest tests/test_market_agent.py tests/test_market_rag.py -q
```

테스트는 조건부 Web Search, 병렬 결과 키 분리, 페이지/출처 추적, unknown 처리, 선택 영역 재실행, 리뷰 1회 제한, PDF 페이지 예산과 dense retrieval 인터페이스를 검증합니다.
