# 06 Investment Review Agent

네 분석 결과를 검증하고, 보완 요청·점수 계산·판정·선정·보고서 생성을 담당합니다. (담당: 김민솔)

## 구조

```text
investment_review/
  contract.py          # 06 자체 출력 계약 (InvestmentReview 등)
  policy.py            # proposed-2.0 정책값
  validate.py          # 취합·교차 검증
  review_requests.py   # Agent별 형식으로 보완 요청 생성
  gates.py             # G01·G02, 필수 결측
  scoring.py           # C1~C6, 총점
  judge.py             # 최종 판정·reason_codes
  selection.py         # 정렬·K개 선정
  nodes.py             # LangGraph 노드 래퍼
  adapters/            # 02~05 원래 형식 읽기 → AgentResult, stub(결과 없음)
  report/              # context_builder → writer → checker → renderer
  tests/fixtures/      # 02~05 실제 코드로 만든 출력 샘플 (build_fixtures.py)
```

## 흐름

```text
기업별: adapters → validate → (review_requests → 보완 1회) → gates → scoring → judge
전체:   selection → report
```

## 읽는 형식

어댑터는 각 Agent 출력을 바꾸지 않고 읽어 `AgentResult`(점수·게이트·이슈·우려·실사 질문·미확인·출처)로 뽑습니다. blocking 판단은 하지 않습니다.

| Agent | State 키 | 형식 | 06이 쓰는 값 | 보완 요청 형식 |
|---|---|---|---|---|
| 02 임상 | `clinical_analysis` | `{company_id: Envelope}` | `criteria_inputs.C1`, `red_flags` CL02 → G01 | list, `target_agent="clinical"` |
| 03 시장 | `market_analysis` | Envelope | `criteria_inputs.C2·C3·C4`, `business_concerns` | `review_requests` 중 `target_agent="market"` |
| 04 실적 | `traction_analysis` | Envelope | `criteria_inputs.C5`, RF1~RF4, `open_questions` | list, `target_agent="traction"` |
| 05 Risk | `risk_analysis`, `references` | `risk-company-3` | `areas` 실사 질문·미확인·`risk_signal` | `review_requests["risk"]` dict, `attempt=1` |

- **C6 (06 자체 규칙 `risk-signal-v1`)**: 05 출력에 OP1~OP5가 없어 영역별 위험 신호(`kind=risk_signal`) 수로 채점합니다. 관찰이 있는 영역만 신호 0건=5, 1건=3, 2건 이상=1로 매겨 평균합니다. 관찰이 있는 영역이 하나도 없으면 null입니다(자료 없음 ≠ 위험 없음). 설계서 C6(OP1~OP5)과 다른 규칙입니다.
- **G02**: 05 분석이 실행됐으면(`complete`·`partial`) `clear`, 실패·`no_evidence`면 `not_checked`. 05 출력에 현재 중단을 확정하는 필드가 없어 `confirmed`는 나오지 않습니다.
- G01: 임상 분석 실패·규제 조사 미완료면 `not_checked`, CL02 confirmed면 `confirmed`, candidate면 `unresolved`, 그 외 `clear`.

## 검증과 보완 요청

`validate.py`가 어댑터 결과를 대조해 이슈를 만들고, `review_requests.py`가 담당 Agent별로 묶어 **각 Agent의 ReviewRequest 형식 그대로** 요청을 만듭니다. 기업당 1라운드입니다.

| 검사 | 판단불가(blocking) | 보완 요청 |
|---|---|---|
| `company_mismatch` 결과 company_id ≠ 심사 대상 | O | X (입력 오류) |
| `as_of_mismatch` 기준일 불일치 | O | O |
| `unknown_evidence` 점수가 결과에 없는 근거 ID 참조 | O | O |
| `no_evidence` 점수에 근거 ID 없음 | O | O |
| `future_source` 기준일 이후 발행 출처 | X (경고) | O |
| `score_unknown` 적용 항목 점수 미확인 | X (judge가 처리) | O |

- 분석 실패(`failed`)·결과 없음(`not_run`) Agent에는 보완 요청을 보내지 않습니다.
- 시장 요청은 `dimensions`로 재실행 영역을 지정합니다: C2=`size_growth`, C3=`demand`+`commercialization`, C4=`monetization`.
- 요청 형식은 테스트에서 각 팀원의 pydantic 모델(`ReviewRequest`)로 검증합니다.

## 판정 규칙 구현 메모

- 게이트(G01·G02)가 입력에 없으면 `not_checked`로 보고 판단불가 처리합니다.
- 모집단에서 적용 항목인데 기업별로 `not_applicable`이 오면 `unknown`으로 바꿉니다. 불리한 항목을 빼고 재가중하지 않기 위해서입니다.
- 총점은 소수 6자리로 반올림해 60점 경계가 부동소수 오차로 흔들리지 않게 합니다.

## 테스트

샘플은 유료 API 없이 02~05 실제 코드를 실행해 만듭니다. 팀원 코드가 바뀌면 다시 생성합니다.

```bash
python -m agents.investment_review.tests.fixtures.build_fixtures
```


```bash
python -m unittest discover -s agents/investment_review/tests -t . -v
```
