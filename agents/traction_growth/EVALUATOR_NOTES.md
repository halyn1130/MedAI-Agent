# 실적·성장성 — 수집/평가 분리 기록 (2026-10-13)

## 결정
- Orchestrator-Workers 구조로 바꾸면서 **수집은 앞단(01 공통 데이터 수집)**, 실적·성장성 워커는 **평가만** 담당.
- 2,228줄 단일 파일(`agent.py`)을 역할별로 나눔. 코드 내용은 옮기기만 하고 로직은 바꾸지 않음.

| 파일 | 역할 | 앞으로 |
|---|---|---|
| `common.py` | 공통: 환경변수·이름 정규화·검색 로그 형식·호출 예산·캐시 스위치·날짜·프로필·출처 유형 분류·Ctx/State | 수집·평가 양쪽에서 import |
| `collect.py` | **수집**: OpenDART·뉴스(네이버·Tavily)·국민연금 클라이언트, 수집 노드 3개, 검색 계획, `collect_traction_inputs()` | **01 공통 수집 노드로 이동** |
| `rules.py` | 판정 규칙 (단계·성장·추이·기대치·RF·C5) 순수 함수 | 평가 쪽 |
| `evaluator.py` | **평가**: LLM 근거 추출·근거 조립(Builder)·판정 조립(assemble)·평가 노드 `traction_evaluate_node`·CLI | Orchestrator 워커로 등록 |
| `agent.py` | 호환용: 수집+평가 통합 실행(`run_traction`, `traction_growth_node`, 기존 CLI), `make_ctx()` | 메인 그래프 전환 후 정리 가능 |

## 연결 방식
```
collect.py: make_ctx(...) → collect_traction_inputs(ctx) → traction_inputs {docs, facts, status, search_log, notes}
evaluator.py: traction_evaluate_node(state["traction_inputs"]) → traction_analysis (AnalysisEnvelope, 기존과 동일)
```
- 재분석 라운드에서 `status` 키가 빠지면 "이번에 재수집 안 함 → 이전 상태 유지" (최초 평가에서는 not_reviewed).
- LLM 추출(`extract`)은 평가 쪽에 둠: 원문 해석이지 조사가 아님. facts만 오면 LLM 호출 없음.

## 검증 (가짜 클라이언트·가짜 LLM으로 외부 호출 없이 고정 비교)
1. 분리 전후 `run_traction`(수집+평가) 결과 동일 — 최초·보완 라운드, 2개 기업.
2. 분리 전후 평가 노드 결과 동일 — 저장된 42개 기업 전체.
3. `collect_traction_inputs → evaluate_traction` 분리 실행 == 통합 실행 — 최초 동일, 보완은 limitations 문구만 다름(의도).
4. `agents/investment_review/tests`: 97 통과, 1 실패(보고서 PDF 변환 라이브러리 미설치 — 실적 코드와 무관).
- 실제 외부 API·실제 LLM 호출 경로는 이 검증에 포함되지 않음 (코드 이동만 했으므로 동작 동일 예상, 미확인).

## 단순 워커화 (2026-10-13 오후)
**결정:** 워커 노드는 판단하지 않는다. 호출되면 주어진 입력으로 정확히 한 번 평가해서 반환.

| 제거한 노드 내부 판단 | 이전 코드 | 넘겨받을 곳 |
|---|---|---|
| 결과가 있으면 건너뛰기 | `return {} if prev` | Orchestrator: 워커를 보낼지 결정 |
| 재분석 횟수 상한 | `review_round > MAX_REVIEW_ROUNDS` | Orchestrator State `replan_round`·`max_replans` (종료 보장) |
| 같은 요청 중복 제거 | `request_id in done` | Orchestrator |
| 요청 여러 개 반복 평가 | `for r in reqs` | Orchestrator가 1건씩 Send |
| 이전 결과 없을 때 최초+재평가 2회 | `env = prev or evaluate(...)` | 이제 ValueError → Orchestrator Fallback |

- 입력 키 변경: `review_requests`(목록) → **`traction_request`(1건)**. `review_requests`는 더 이상 읽지 않음.
- 근거: 과제 "재실행 여부는 Orchestrator가 결정", Send 1회 = 평가 1회로 트레이스가 명확해짐.
- 검증: 호출당 평가 1회(최초·재호출·재분석·같은 요청 재전송 모두), round=5도 상한 검사 없이 실행, 이전 결과 없는 재분석은 ValueError, 42개 기업 판정 동일.
- 주의: 기존 경로 `agent.traction_growth_node`(수집+평가 통합)는 이전 방식 그대로 — 메인 그래프 전환 전 호환용.

## 남은 일 (다른 담당과 합의)
- 01 수집 담당: `collect.py`를 가져가 `traction_inputs` 형식으로 넘겨주기.
- Orchestrator 담당: `investment_review/nodes.py`에서 `traction_inputs` 전달 + `traction_evaluate_node` 호출.
- 전환 완료 후 `agent.py`의 통합 경로 제거 여부 결정.
