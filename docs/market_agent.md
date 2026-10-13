# 시장·사업성 평가 Node

시장 Worker는 공통 입력의 `company_profile`과 `dataset`만 사용하여 시장 성장·수요·도입·수익화를 한 번 평가하고 결과를 직접 반환합니다. LLM으로 자료를 해석할 수 있으며, 점수는 기존 Python 기준으로 계산합니다.

Worker는 웹 검색, 크롤링, RAG 질의·검색, 파일·인덱스 로딩, 모델 호출 재시도를 하지 않습니다. 이전 결과 존재 여부, 처리한 요청 ID, 재검토 라운드에 따라 실행을 생략하지 않습니다. 실행 대상 선택과 반복 횟수는 Orchestrator가 관리합니다.

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

`dataset.version`은 필수입니다. `sources`의 각 항목에는 고유한 `source_id`를 넣고 제목·URL·발행일·지역·세그먼트를 함께 전달합니다. `records`에는 제공할 원문을 담습니다.

```json
{
  "record_id": "report-1:page-7",
  "source_id": "report-1",
  "text": "평가에 사용할 실제 원문",
  "locator": "page 7",
  "page": 7,
  "geography": "KR",
  "segment": "clinical decision support software"
}
```

`source_ids` 목록도 사용할 수 있습니다. `company_id`나 `agent`가 있는 레코드는 현재 기업·시장 Worker에 해당하는 자료만 평가합니다. `dimension`은 `size_growth`, `demand`, `commercialization`, `monetization` 중 하나로 지정할 수 있고, 생략하면 모든 영역에 전달합니다. 기준일 이후 자료는 제외하고, 출처를 연결할 수 없는 레코드는 `errors`에 기록합니다.

Orchestrator는 기업에 사용할 자료와 LLM 입력량을 준비하여 전달합니다. Worker는 전달된 레코드에서 자체 검색으로 자료를 더 찾지 않습니다. 재실행에도 새로 전달된 `dataset`만 근거로 사용하며, `previous_result`의 출처·근거·점수를 새 결과에 합치지 않습니다. 이전 결과는 판단 비교와 결과 버전 부여에만 쓰입니다.

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

`MarketNode(reasoner=...)`로 평가 모델을 주입할 수 있습니다. `MarketAgent` 이름은 같은 Node의 호환 별칭입니다. `build_market_graph()`가 필요하면 `START → evaluate → END` 한 노드 그래프를 반환합니다.

기존 기업 프로필만 전달하던 호출과 `market_analysis` 래퍼를 읽던 호출부는 위 계약으로 연결해야 합니다. 이번 변경은 시장 Worker에만 적용합니다.

## 실행 및 기존 데이터셋 ZIP

LLM 호출에는 `OPENAI_API_KEY`, `MARKET_MODEL`만 필요합니다. `.env` 설정은 기본 Node를 만들 때 읽습니다.

```bash
python -m agents.market --input examples/market/input.json --output outputs/market_result.json
python -m agents.market --input examples/market/input.json --output outputs/market_demo.json --demo
python -m pytest tests/test_market_agent.py tests/test_market_rag.py -q
```

`examples/market/input.json`에는 실제 시장 자료의 출처 6개·원문 레코드 193개를 담은 `dataset`이 포함되어 있습니다. 기업 프로필은 `Sample Health AI`라는 가상 기업이며, 실제 기업 평가에는 Orchestrator가 준비한 프로필과 해당 평가에 사용할 레코드를 전달합니다. `--demo`는 LLM을 호출하지 않고 미확인 점수를 반환합니다.

공유 데이터셋은 [data/market_evaluation](../data/market_evaluation/README.md)에 있습니다. `dataset.json`은 Worker 입력의 `dataset` 값으로 바로 쓸 수 있고, `source_documents.zip`에는 원본 PDF 6개와 메타데이터도 포함됩니다. 저장된 오프라인 실행 결과는 [offline_result.json](../examples/market/offline_result.json), 검증 요약은 [offline_validation.json](../examples/market/offline_validation.json)에서 확인할 수 있습니다. 이 결과는 API 없이 입력·출력·근거 연결을 검증한 것이며 실제 LLM 평가 결과는 아닙니다.

기존 PDF 6개는 물리적으로 202쪽이며, `metadata.csv`에서 2쪽을 제외한 200쪽이 대상입니다. 이 중 텍스트가 추출된 193쪽을 `dataset.json`의 레코드로 담고, 추출 텍스트가 없는 7쪽도 PDF 원본 안에 보존합니다. ZIP에는 PDF 원본, 메타데이터, 페이지별 원문을 담은 `dataset.json`, 입출력 JSON Schema, 파일별 SHA-256을 기록한 `manifest.json`이 들어갑니다. 확인일이 기존 메타데이터에 없으면 `checked_at=null`로 보존합니다.

```bash
python -m agents.market.package_dataset --output ~/Desktop/MedAI_market_dataset_2026-10-13.zip
```

이 명령은 저장된 파일을 오프라인으로 내보내는 준비 도구이며 Worker 실행 중 호출되지 않습니다. 기존 RAG 인덱스 준비 도구도 Worker와 분리되어 있습니다.
