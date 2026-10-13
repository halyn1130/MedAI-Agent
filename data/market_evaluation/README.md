# 시장 평가 Node 공유 데이터셋

새 시장 Worker 계약으로 실행할 수 있는 고정 입력과 원본 자료를 함께 공유합니다.
외부 검색이나 재수집 없이 기존 시장 자료를 변환했습니다.

| 파일 | 내용 |
|---|---|
| `dataset.json` | `dataset.version`, `sources`, `records` 형식의 평가용 데이터 |
| `source_documents.zip` | 원본 PDF 6개, 기존 메타데이터, 동일한 `dataset.json`, 입출력 JSON Schema |
| `source_manifest.json` | ZIP 내부 파일의 크기·SHA-256과 페이지 통계 |

출처는 한국보건산업진흥원의 「의료분야 인공지능 활용 실태 조사 분석 연구」이며, 기존 메타데이터에 기록된 발행일은 2026-02-26입니다. 원문 URL과 지역·세그먼트·페이지 위치를 보존합니다. 자료는 산업 공통 근거이며 특정 기업의 실제 실적이나 제품을 검증한 데이터가 아닙니다.

- 데이터셋 버전: `market-dataset-v1`
- 출처: 6개
- 원본: 202쪽, 메타데이터가 지정한 평가 대상: 200쪽
- 추출된 텍스트: 193쪽 → 193개 DataRecord
- 텍스트가 추출되지 않은 대상 페이지 7쪽도 PDF 원본에 보존
- 확인일이 기존 메타데이터에 없으면 `checked_at=null`로 유지

## 재현 방법

프로젝트 루트에서 실행합니다.

```bash
python -m agents.market \
  --input examples/market/input.json \
  --output outputs/market_offline_result.json \
  --demo
```

`examples/market/input.json`에는 이 데이터셋 전체를 포함한 공통 Worker 입력이 들어 있습니다. 기업 프로필의 `Sample Health AI`는 형식 검증용 가상 기업입니다. 실제 기업을 평가할 때는 Orchestrator가 해당 기업 프로필과 사용할 레코드를 준비하여 전달합니다.

저장된 실행 결과는 [offline_result.json](../../examples/market/offline_result.json), 검증 요약과 파일 해시는 [offline_validation.json](../../examples/market/offline_validation.json)에서 확인할 수 있습니다. 이 실행은 API를 호출하지 않는 오프라인 계약 검증입니다. 모델이 없으므로 평가 점수는 `null`/`unknown`, 실행 상태는 `partial`로 반환되며 오류는 없습니다. 실제 LLM 평가 결과로 해석하지 않습니다.

원본 ZIP을 풀면 기존 `data/market_rag/` 구조의 PDF와 메타데이터를 확인할 수 있습니다. Worker 자체는 PDF나 인덱스를 로딩하지 않으며, 입력의 `dataset`만 사용합니다.
