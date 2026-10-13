# 시장·사업성 평가 Node

시장 Worker는 고정된 시장 PDF 인덱스에서 관련 문맥을 RAG로 검색한 뒤 시장 성장·수요·도입·수익화를 한 번 평가하고 결과를 직접 반환합니다. LLM은 선별된 자료를 해석하며, 점수는 기존 Python 기준으로 계산합니다.

Worker는 웹 검색, 크롤링, 외부 자료 신규 수집, 모델 호출 재시도를 하지 않습니다. 로컬 PDF RAG는 Worker 내부에서 수행하며 자료가 부족하더라도 외부 검색으로 보충하지 않습니다. 이전 결과 존재 여부, 처리한 요청 ID, 재검토 라운드에 따라 실행을 생략하지 않습니다. 실행 대상 선택과 반복 횟수는 Orchestrator가 관리합니다.

```text
company_profile → 고정 PDF의 BGE-M3·Chroma 검색 → 영역별 문맥 선별 → LLM 해석 → Python 채점
```

RAG 질의는 제품·용도·고객·구매자·지역과 평가 영역을 이용해 코드에서 생성합니다. 질의 생성을 위한 추가 LLM 호출은 없습니다. 각 영역의 검색 결과는 청크 ID로 중복 제거하고 유사도 순으로 선택합니다. 기준일 이후 자료, 다른 기업·Agent 자료, 페이지 위치가 없는 청크는 제외합니다.

기본 제한은 질의당 상위 5개, 영역당 최대 10개 청크·원문 16,000자, 청크당 최대 4,000자입니다. 제한은 아래 환경변수로 조정합니다. 글자 수 제한이며 모델 토큰 수 제한은 아닙니다.

| 환경변수 | 기본값 |
|---|---:|
| `MARKET_RAG_TOP_K` | 5 |
| `MARKET_RAG_MAX_CHUNKS` | 10 |
| `MARKET_RAG_MAX_CHUNK_CHARS` | 4000 |
| `MARKET_RAG_MAX_DIMENSION_CHARS` | 16000 |
| `MARKET_RAG_MAX_CONTEXT_CHARS` | 6000 |

인덱스가 없거나 비어 있거나 검색에 실패하면 명시적인 오류를 반환합니다. 전체 PDF를 LLM에 넘기는 fallback은 없습니다. 검색은 성공했지만 해당 영역의 근거가 없으면 점수는 `unknown`으로 남깁니다.

## 입력 계약

```python
worker_input = {
    "run_id": "run-2026-09-30",
    "company_id": "c001",
    "as_of": "2026-09-30",
    "schema_version": "2.0",
    "criteria_version": "proposed-2.0",
    "company_profile": {},
    "dataset": {"version": "market-dataset-v1", "sources": [], "records": []},
    "previous_result": None,
    "review_request": None,
}
```

공통 입력·출력 형식은 그대로 유지합니다. `dataset.version`은 필수입니다. `sources`를 전달하면 해당 출처 범위에서 검색하며, 생략하면 고정 인덱스의 출처를 사용합니다. 각 출처의 `source_id`는 인덱스의 `document_id`와 연결합니다. 원문 PDF의 모든 페이지를 `records`로 전달할 필요는 없습니다.

RAG로 만들어지는 레코드는 아래처럼 출처와 PDF 페이지 위치를 보존합니다. `dimension`은 검색한 평가 영역입니다.

```json
{
  "record_id": "report-1:page-7",
  "source_id": "report-1",
  "dimension": "demand",
  "text": "평가에 사용할 실제 원문",
  "locator": "page 7",
  "page": 7,
  "geography": "KR",
  "segment": "clinical decision support software"
}
```

기업별 보충 자료는 `records`에 `dimension="context"` 또는 현재 `company_id`를 명시하여 전달할 수 있습니다. 이 자료는 총 6,000자로 제한하고 기업 프로필 보완에만 사용합니다. 산업 공통 PDF 문맥은 기업별 사실을 보완하는 자료로 사용하지 않습니다.

이미 RAG로 선별한 자료를 평가하는 경우에만 `MarketNode(use_rag=False)` 또는 CLI의 `--prepared-input`을 사용합니다. 이 경로에서는 `source_ids` 목록과 영역별 `dimension`을 사용할 수 있고, 생략한 영역은 모든 평가 영역에 전달됩니다. 전달량은 호출자가 제한해야 합니다.

재실행에도 같은 고정 PDF 인덱스에서 관련 문맥을 선별합니다. `previous_result`의 출처·근거·점수를 새 결과에 합치지 않습니다. 이전 결과는 결과 버전과 변경 비교에 사용하고, LLM에는 이전 영역별 점수·상태·짧은 판단 설명만 전달합니다. 이전 PDF 원문이나 전체 evidence를 재검토 프롬프트에 넣지 않습니다.

## 반환 계약

`market_analysis` 래퍼 없이 다음 객체를 직접 반환합니다.

```json
{
  "run_id": "run-2026-09-30",
  "company_id": "c001",
  "as_of": "2026-09-30",
  "schema_version": "2.0",
  "criteria_version": "proposed-2.0",
  "agent": "market",
  "dataset_version": "market-dataset-v1",
  "result_version": 1,
  "analysis_status": "partial",
  "data": {},
  "sources": [],
  "evidence": [],
  "findings": [],
  "missing_items": [
    {
      "missing_id": "c001:market:mi-example",
      "item": "comparable_cagr",
      "reason": "제공 자료만으로 판단할 수 없음",
      "related_criterion_ids": ["C2"],
      "required_for_assessment": true
    }
  ],
  "coverage": [],
  "review_responses": [],
  "errors": []
}
```

`data`에는 `market_scope`, `supplemental_context`, 네 영역의 `dimensions`, `market_metrics`, `criteria_inputs.C2/C3/C4`, `overall_market_score`, `market_summary`, `business_concerns`가 들어갑니다. 근거가 부족하면 점수는 `null`, `score_status`는 `unknown`으로 유지하고 `missing_items`를 반환합니다.

`analysis_status`는 평가가 완성되면 `complete`, 결측 또는 일부 오류가 있으면 `partial`, 네 영역의 평가 호출이 모두 실패하면 `failed`입니다. 한 영역의 호출 실패를 다시 호출하지 않으며 다른 영역의 결과는 보존합니다. SDK의 자동 재시도도 `max_retries=0`으로 껐습니다.

`previous_result`가 있으면 `result_version`에 1을 더합니다. 이는 결과 식별용 버전이며 실행 횟수 제한이 아닙니다. `review_request`는 `request_id`가 필요하며, 해당 요청에 대한 변경 요약을 `review_responses`에 반환합니다. 개별 질문에 대한 확정 답변을 별도로 생성하지 않으므로 재검토 해결 상태는 `unresolved`로 남깁니다. 추가 평가 여부는 Orchestrator가 판단합니다.

## Orchestrator 연결

```python
from agents.market import market_node

result = market_node(worker_input)
state["market_analysis"] = result  # 공통 State 반영은 Orchestrator에서 수행
```

`MarketNode(reasoner=...)`로 평가 모델을, `MarketNode(preparer=...)`로 로컬 RAG 구현을 주입할 수 있습니다. `MarketAgent` 이름은 같은 Node의 호환 별칭입니다. `build_market_graph()`가 필요하면 `START → evaluate → END` 한 노드 그래프를 반환하며 그 노드 안에서 로컬 RAG와 평가를 수행합니다.

기존 06 그래프가 `company_profile`, `market_analysis`, `review_requests`를 전달하는 호출도 시장 노드 내부에서 변환하며, 그 호출에만 기존 `market_analysis` 래퍼로 반환합니다. 공통 v2 입력에는 직접 WorkerResult를 반환합니다. 다른 Worker와 Orchestrator의 코드는 변경하지 않습니다.

## 실행 및 기존 데이터셋 ZIP

LLM 호출에는 `OPENAI_API_KEY`, `MARKET_MODEL`이 필요합니다. 로컬 RAG는 `BAAI/bge-m3`와 `data/vectorstore/market/`의 기존 Chroma 인덱스를 사용합니다. 상대 경로 환경변수는 프로젝트 루트를 기준으로 해석합니다. `.env` 설정을 읽되 API 키는 결과에 포함하지 않습니다.

처음 설치할 때 고정 원본 ZIP을 풀고 인덱스를 생성합니다. 이 과정에서 임베딩 모델도 준비합니다. Worker 실행 중에는 캐시된 모델만 읽으며, 모델 다운로드·웹 검색·외부 자료 재수집을 하지 않습니다.

```bash
unzip data/market_evaluation/source_documents.zip 'data/market_rag/*' -d .
python -m rag.market.build_index --rebuild
```

```bash
python -m agents.market --input examples/market/input.json --output outputs/market_result.json
python -m agents.market --input examples/market/input.json --output outputs/market_demo.json --demo
python -m agents.market --input examples/market/input.json --output outputs/market_demo.json --prepared-output outputs/market_selected_input.json --demo
python -m pytest tests/test_market_agent.py tests/test_market_rag.py tests/test_market_preparation.py -q
```

`examples/market/input.json`에는 실제 시장 자료의 출처 6개와 데이터셋 버전만 넣고, 원문 페이지는 인덱스에서 검색합니다. 기업 프로필은 `Sample Health AI`라는 가상 기업입니다. `--demo`도 로컬 RAG를 수행하며 LLM 호출 없이 미확인 점수를 반환합니다. `--prepared-output`으로 실제 선별된 Worker 입력을 확인할 수 있습니다.

공유 원본은 [data/market_evaluation](../data/market_evaluation/README.md)에 있습니다. `dataset.json`은 전체 페이지의 보관용 원문이며, 기본 Worker는 관련 RAG 청크만 평가합니다. `source_documents.zip`에는 원본 PDF 6개와 메타데이터도 포함됩니다. 로컬 RAG의 오프라인 실행 결과는 [offline_result.json](../examples/market/offline_result.json), 검증 요약은 [offline_validation.json](../examples/market/offline_validation.json)에서 확인할 수 있습니다. 이 결과는 API 없이 검색·입력·출력·근거 연결을 검증한 것이며 실제 LLM 평가 점수는 아닙니다.

기존 PDF 6개는 물리적으로 202쪽이며, `metadata.csv`에서 2쪽을 제외한 200쪽이 대상입니다. 이 중 텍스트가 추출된 193쪽을 `dataset.json`의 레코드로 담고, 추출 텍스트가 없는 7쪽도 PDF 원본 안에 보존합니다. ZIP에는 PDF 원본, 메타데이터, 페이지별 원문을 담은 `dataset.json`, 입출력 JSON Schema, 파일별 SHA-256을 기록한 `manifest.json`이 들어갑니다. 확인일이 기존 메타데이터에 없으면 `checked_at=null`로 보존합니다.

```bash
python -m agents.market.package_dataset --output ~/Desktop/MedAI_market_dataset_2026-10-13.zip
```

이 명령은 저장된 파일을 오프라인으로 내보내는 준비 도구이며 Worker 실행 중 호출되지 않습니다. 기존 RAG 인덱스 준비 도구도 Worker와 분리되어 있습니다.
