# 실적·성장성 — Orchestrator 전환 기록 (2026-10-13)

## 1. 파일 구성 (수집/평가 분리)
| 파일 | 역할 | 앞으로 |
|---|---|---|
| `common.py` | 공통: 환경변수·이름 정규화·검색 로그·호출 예산·캐시 스위치·날짜·프로필·출처 유형 분류·Ctx/State | 수집·평가 양쪽에서 import |
| `collect.py` | **수집**: DART·뉴스(네이버·Tavily)·국민연금, 수집 노드 3개, 검색 계획, `collect_dataset()` → 공통 데이터셋 | **01 공통 수집 노드로 이동** |
| `rules.py` | 판정 규칙 (단계·성장·추이·기대치·RF·C5) 순수 함수 | 평가 쪽 |
| `evaluator.py` | **Worker**: LLM 근거 추출·근거 조립·판정 조립·공통 계약 변환·`traction_worker_node` | Orchestrator 워커로 등록 |
| `agent.py` | 호환용 수집+평가 통합 실행(`run_traction`, `traction_growth_node`, 기존 CLI), `make_ctx()` | 메인 그래프 전환 후 정리 |

## 2. 단순 워커 원칙
- 호출되면 주어진 데이터로 **정확히 한 번** 평가. 결과 재사용·건너뛰기, 재검토 횟수 상한, 중복 요청 제거, 반복 처리 **없음** → Orchestrator 몫.
- 자체 검색·수집·재시도 루프 없음. LLM은 원문 레코드 해석에만.
- 입력 오류·실행 오류도 예외 대신 `analysis_status=failed` + `errors`로 반환 (누락과 구분).

## 3. 공통 State 계약 적용 (docs/common_state.md 초안)
**입력 (4장):** `{run_id, company_id, as_of, schema_version, criteria_version, company_profile, dataset, previous_result, review_request}`
- 노드는 State에서 `worker_results["traction"]` → previous_result, `review_requests["traction"]` → review_request를 그대로 꺼내 전달 (판단 없음).
- `dataset.records[].content`
  - dict `{kind, record, status, statement, excerpt, event_date}` → 구조화 사실 (kind: revenue·contract·activity·headcount·customer)
  - 문자열 → 원문. LLM 추출, 발췌가 원문에 없으면 버림. LLM 매핑용으로 내부에서만 `record://{record_id}` URL 사용.
- `tags`: `TRACTION_TAGS`(traction·financial·revenue·contract·news …) 중 하나 있으면 읽음. 태그 없는 데이터셋이면 전체. 출처 유형 태그(official·company·partner·news)는 독립 확인 판단에 사용.

**반환 (5장):** `{"worker_results": {"traction": WorkerResult}}`
| 필드 | 변환 |
|---|---|
| sources | 근거에 실제 쓴 데이터셋 Source를 **ID·내용 그대로** |
| evidence | `evidence_id, record_id, source_id, excerpt, locator` (내부 근거 → record 연결 유지) |
| findings | 기존과 동일 구조 |
| missing_items | 내부 결측 → `missing_id, item, reason, related_criterion_ids, required_for_assessment` (C5가 미확정일 때 매출·계약 결측만 필수) |
| coverage | 영역 6개(disclosure·revenue·contract·activity·customer·headcount) × `reviewed/partial/not_reviewed` + 관련 record_ids |
| review_responses | 질문별 `request_id, question_id, answer, finding_ids, resolved` — resolved = 대상 판단이 근거로 뒷받침됨 (결론 변경 여부와 무관) |
| errors | `DATASET_VERSION_MISMATCH`, `INVALID_INPUT`, `LLM_NOT_CONFIGURED`, `LLM_EXTRACTION_*`, 예외명 |
| data | `schema.TractionData` 그대로 (심사 어댑터가 `data.criteria_inputs.C5` 등을 읽음) |

**의미가 바뀐 부분 (중요)**
- 계약에 "수집 완료 여부"가 없음 → **reviewed = 제공 자료 검토**. 원문이 있어도 뉴스 범위는 partial로 보고, `계약 없음(RF1)`·`none_reason=no_recognized_evidence` 같은 **부재 판정은 하지 않음**.
- `analysis_status`: failed = 입력·실행 오류 / complete = 6개 영역 모두 reviewed이고 오류 없음 / 그 외 partial.
- 재검토는 이전 결과 내부를 병합하지 않고 **같은 데이터셋으로 처음부터 다시 판정** → 이전 findings와 비교해 답변. previous_result가 있으면 result_version + 1.
- 수집 상태·검색 기록은 데이터셋에 넣지 않고 `collect_dataset()`의 두 번째 반환값(수집 로그)으로 분리 (7장: 실행 이력은 로그 계층).

## 4. 검증 (외부 호출 없이 가짜 클라이언트·가짜 LLM / 저장 결과)
1. 저장된 42개 기업 → 데이터셋 변환 → Worker: 단계·성장·C5 **차이 0/42**, RF·none_reason·기대치 **차이 0/42**.
2. findings → evidence → record/source 연결 모두 데이터셋에 존재.
3. `collect_dataset → evaluate_worker` == 기존 통합 실행: 단계·성장·고용 추이·C5·계약 건수·RF 동일. analysis_status만 complete → partial (의도: 외부 검색 완료를 주장하지 않음).
4. 기존 통합 경로 `run_traction` 결과 동일 (evidence에 `record_id: null` 필드만 추가).
5. 재검토(v2·질문별 답변), 데이터 버전 불일치·기업명 없음 → failed+errors, 원문+LLM 미설정 → partial+`LLM_NOT_CONFIGURED`, 다른 태그(clinical) 레코드 제외.
6. `agents/investment_review/tests` 97 통과, 1 실패(PDF 변환 라이브러리 미설치 — 무관).
- 실제 외부 API·실제 LLM 경로는 미검증.

## 5. 팀 합의 필요 (common_state.md 9장과 연결)
- `company_profile`에 `invest_stage` 포함 여부 — 없으면 기대치·RF4 판단 불가(결측으로 기록됨).
- 구조화 레코드 content 형식(`{kind, record, …}`)과 태그 규칙을 수집 담당과 확정.
- 심사 어댑터(`investment_review/adapters`)의 missing_items 읽기 필드가 새 계약과 다름(`impact/field/cause` → `item/reason`) — 취합 노드 담당이 새 계약 기준으로 수정 필요.
- 원문 레코드가 많을 때 LLM 비용 — 수집 노드에서 태그로 배분 범위 조절.
