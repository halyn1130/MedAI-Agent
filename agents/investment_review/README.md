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
  adapters/            # traction(04)·risk(05) 원래 형식 읽기, stub(02·03 미구현)
  report/              # context_builder → writer → checker → renderer
  tests/fixtures/      # 가짜 Envelope 입력
```

## 흐름

```text
기업별: adapters → validate → (review_requests → 보완 1회) → gates → scoring → judge
전체:   selection → report
```

## 읽는 형식

| Agent | State 키 | 형식 | 보완 요청 형식 |
|---|---|---|---|
| 04 실적 | `traction_analysis` | `agents/traction_growth/schema.py`의 AnalysisEnvelope | `review_requests` list, `target_agent="traction"` |
| 05 Risk | `risk_analysis`, `references` | `risk-company-3` (`areas`, `passages`) | `review_requests["risk"]` dict, `attempt=1` |
| 02 임상, 03 시장 | — | 코드 없음 → stub | 미정 |

## 판정 규칙 구현 메모

- 게이트(G01·G02)가 입력에 없으면 `not_checked`로 보고 판단불가 처리합니다. 02 임상 결과가 없으면 G01을 조사하지 못한 것이므로, 현재는 모든 기업이 undetermined가 되는 것이 정상입니다.
- 모집단에서 적용 항목인데 기업별로 `not_applicable`이 오면 `unknown`으로 바꿉니다. 불리한 항목을 빼고 재가중하지 않기 위해서입니다.
- 총점은 소수 6자리로 반올림해 60점 경계가 부동소수 오차로 흔들리지 않게 합니다.

## 테스트

```bash
python -m unittest discover -s agents/investment_review/tests -t . -v
```
