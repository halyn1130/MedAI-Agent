# Risk — 전달 데이터 기반 단일 평가 노드

루트 `common_state.md`의 Worker 입출력·재검토 계약을 사용합니다. 외부 자원 의존, 핵심 역할 연속성, 운영 사건을 평가합니다. 자료 부족은 위험 없음이 아니며 원문 연결은 사실 검증을 대신하지 않습니다.

## 실행 경계

```text
별도 수집·로딩 → WorkerInput → Risk 평가 1회 → WorkerResult
                                  ↑
              Orchestrator의 재검토 요청 + 이전 결과
```

- Worker는 전달받은 `dataset`만 읽습니다. manifest 로딩, 검색, 재수집, 내부 재검토 루프가 없습니다.
- 호출당 모델 요청 최대 1회이며 자동 재시도와 모델 응답 캐시는 없습니다. 관련 원문 구간이 없으면 호출하지 않습니다.
- 기존 `collection/`은 별도 수집 도구입니다. 고정 자료 로딩·공통 입력 변환은 `collection/prepare_input.py`에서 수행합니다.
- `evidence.py`는 현재 평가에 쓰는 근거 선택·응답 검증 함수입니다. 수집 전용 출처 모델은 `collection/schema.py`에 있습니다. 구형 Agent·배치 평가·캐시 실행 코드는 제거했습니다.

## 입력과 반환

```python
from agents.risk import evaluate_risk, risk_worker_node

worker_input = {
    'run_id': 'run-2026-09-30',
    'company_id': 'c001',
    'as_of': '2026-09-30',
    'schema_version': '2.0',
    'criteria_version': 'proposed-2.0',
    'company_profile': {'legal_name': '기업명'},
    'dataset': {'version': 'dataset-v1', 'sources': [], 'records': []},
    'previous_result': None,
    'review_request': None,
}
result = evaluate_risk(worker_input)  # WorkerResult
update = risk_worker_node(worker_input)  # {'worker_results': {'risk': result}}
```

반환 필드는 `run_id`, `company_id`, `as_of`, `schema_version`, `criteria_version`, `agent`, `dataset_version`, `result_version`, `analysis_status`, `data`, `sources`, `evidence`, `findings`, `missing_items`, `coverage`, `review_responses`, `errors`입니다.

- `data.areas`에 세 영역의 observations·unknowns·due_diligence_questions를 유지합니다.
- `findings.evidence_ids → evidence.record_id/source_id → dataset.records/sources`로 연결합니다. 인용문은 모델이 작성하지 않고 입력 구간에서 가져옵니다.
- 입력의 출처 ID와 내용을 보존합니다. 같은 출처에 여러 레코드가 있어도 레코드별 근거를 구분합니다.
- 실행 오류·잘못된 입력은 failed, 자료 부족·응답 일부 오류·미해결 질문은 partial입니다. complete는 저위험 판정이 아닙니다.
- result_version은 첫 실행 1, 같은 실행·기업·평가 조건의 이전 결과가 있으면 1 증가합니다.

## 재검토

```python
worker_input['previous_result'] = result
worker_input['review_request'] = {
    'request_id': 'review-risk-01',
    'attempt': 1,
    'reason': '협력 사실에서 의존성을 추정했는지 확인',
    'questions': [{'question_id': 'q01', 'question': '대체 가능성을 판단할 근거가 있는가?'}],
    'target_finding_ids': [],
    'dataset_version': 'dataset-v1',
}
revised = evaluate_risk(worker_input)
```

이전 결과의 기업·실행·기준일·계약 버전·평가 기준을 검증합니다. 해석 재검토는 이전 결과와 요청, 현재 데이터 버전이 같아야 합니다. 데이터가 보완되어 버전이 달라졌다면 `review_request=None`으로 새 데이터를 재평가합니다.

이전 판단과 질문을 한 번의 모델 요청에 전달합니다. 모든 요청 질문에 대해 request_id·question_id·answer·finding_ids·resolved를 반환합니다. 누락·알 수 없는 근거 ID·답변 불가이면 resolved=false와 missing_items를 남깁니다. 출처 연결은 답변의 의미적 정확성을 보증하지 않습니다.

전체 State에서는 `worker_results['risk']`와 `review_requests['risk']`를 자동으로 읽습니다. Worker는 자기 결과만 반환하고 요청 삭제·반복 횟수·수집 분기는 Orchestrator가 관리합니다. 병렬 실행에서는 worker_results를 키별로 병합하는 reducer가 필요합니다.

## 별도 로딩과 CLI

고정 Risk 자료를 사용하려면 평가 전에 입력 파일을 준비합니다.

```bash
python -m agents.risk.collection.prepare_input --company-id <고정데이터ID> --run-id run-2026-09-30 --output outputs/risk_input.json
python -m agents.risk --input outputs/risk_input.json --output outputs/risk_result.json
```

평가 모델 호출에는 `.env`의 `OPENAI_API_KEY`, `RISK_MODEL`이 필요합니다. 실제 수집 CLI는 `python -m agents.risk.collection --help`에서 별도로 확인합니다.

통합 데이터셋 로더를 사용하는 경우 반환된 입력에 run_id·schema_version·criteria_version을 추가하고 전달하세요. Worker 안에서는 로더를 호출하지 않습니다.

## 검토 범위와 이행 주의

- 기업명이 등장하는 원문 주변 최대 40개 구간을 평가합니다. 전체 자료를 읽었다고 해석하면 안 됩니다.
- 미래 발행일 자료, 타 기업으로 명시된 자료, 모델 추출값, 시장 공통 자료는 Risk 근거에서 제외합니다. 기업명이 짧은 경우 사실 관측을 확정하지 않습니다.
- 후보 자료·검색 결과의 기업 일치와 주장의 사실성은 별도 검증이 필요합니다.
- 기존 `RiskAgent(manifest_path=..., max_reviews=...)`와 `risk_analysis/references` 출력 계약은 제거했습니다. `RiskAgent()`는 단일 노드의 호환 이름이고 `build_risk_graph()`는 선택적인 노드 1개짜리 래퍼입니다.
- 기존 investment_review/nodes.py의 Risk 래퍼는 구형 manifest ID·risk_analysis 계약을 사용합니다. 상위 그래프는 공통 dataset 전달 및 worker_results 병합 방식으로 별도 이행해야 합니다. 이번 변경에서 상위 그래프 전체를 재설계하지 않았습니다.

## 검증

```bash
python -m unittest agents.risk.test_graph agents.risk.test_evidence agents.risk.collection.test_collect agents.risk.collection.test_quality
```

가상 모델로 단일 호출, 재검토 질문·응답, 버전 검증, 인용 연결, 자료 없음, 실패 처리, 입력 불변성 및 상위 LangGraph 결과 병합을 검증합니다. 유료 호출·새 자료 수집은 하지 않습니다.
