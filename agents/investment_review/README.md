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
  nodes.py             # LangGraph: 기업별 그래프 + 전체 그래프
  inputs.py            # 기업 목록·저장된 결과 불러오기 (01 대체)
  adapters/            # 02~05 원래 형식 읽기 → AgentResult, stub(결과 없음)
  report/              # context_builder → writer(선택) → checker → renderer, CLI
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

- **C3·C4 (06 자체 규칙 `confirmed-checks-v1`)**: 03 결과의 `criteria_inputs.C3·C4`는 체크가 하나라도 unknown이면 null이라, 06이 `dimensions`의 체크를 직접 셉니다. 영역 점수는 yes 개수이고 unknown은 점수에 넣지 않고 `unconfirmed_checks`·미확인 목록·보완 요청으로 남깁니다. 확인된 체크(yes/no)가 없는 영역은 null이며, C3는 `demand`·`commercialization` 평균이라 둘 중 하나라도 null이면 null입니다.
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
| `unconfirmed_checks` 점수는 나왔지만 unknown 체크가 남음 (C3·C4) | X (경고) | O |

- 분석 실패(`failed`)·결과 없음(`not_run`) Agent에는 보완 요청을 보내지 않습니다.
- 시장 요청은 `dimensions`로 재실행 영역을 지정합니다: C2=`size_growth`, C3=`demand`+`commercialization`, C4=`monetization`.
- 요청 형식은 테스트에서 각 팀원의 pydantic 모델(`ReviewRequest`)로 검증합니다.

## 완화 기준 (기본값)

공개자료만으로는 모든 기업이 판단불가가 되어, 06에서 기준을 낮췄습니다. `policy.py`에서 끌 수 있습니다. `DEFAULT_POLICY`는 `ZERO_FILL_POLICY`(0점 처리), `PARTIAL_POLICY`는 부분 판정, `STRICT_POLICY`는 설계서 기준입니다. G01·G02 미확인이나 blocking 이슈는 어느 기준에서도 판단불가입니다.

| 완화 | 설정 | 내용 |
|---|---|---|
| C2 대체 | `C2_CAGR_FALLBACK` | 03 C2가 null이면 03이 찾은 CAGR 수치의 중앙값을 설계서 구간(5%·10%·15%·20%)으로 채점. 세부시장이 아닐 수 있음을 근거에 표시 |
| C3 한쪽 허용 | `C3_ALLOW_SINGLE_DIMENSION` | 수요·도입 중 확인된 영역만으로 C3 계산 |
| **0점 처리 (기본값)** | `Policy.missing_as_zero=True` | 미확인 적용 항목을 0점으로 채워 총점을 계산 (`score_basis=zero_filled`, 항목별 `zero_filled=True`). 0점으로 채운 항목에는 2점 최소 기준을 적용하지 않고, 60점 기준은 그대로 적용. 켜면 부분 판정은 쓰이지 않음 |
| 부분 판정 | `Policy.min_coverage=0.5` (`PARTIAL_POLICY`) | 게이트·차단 이슈가 없고 채점된 가중치가 50% 이상이면 채점된 항목으로 적격·부적격 판정 (`score_basis=partial`). 60점·2점 기준은 채점된 항목에 그대로 적용 |

## 판정 규칙 구현 메모

- 게이트(G01·G02)가 입력에 없으면 `not_checked`로 보고 판단불가 처리합니다.
- 모집단에서 적용 항목인데 기업별로 `not_applicable`이 오면 `unknown`으로 바꿉니다. 불리한 항목을 빼고 재가중하지 않기 위해서입니다.
- **참고 점수**: 총점이 null이면 채점된 항목만으로 `reference_score`를 계산하고, 쓴 항목(`reference_criteria`)과 가중치 비율(`reference_weight`)을 함께 남깁니다. 판정·순위·선정에는 쓰지 않고 판단불가 사유 옆에 표시만 합니다.
- 총점은 소수 6자리로 반올림해 60점 경계가 부동소수 오차로 흔들리지 않게 합니다.

## 실행 (LangGraph)

`nodes.py`의 `build_graph()`가 기업별 그래프를 병렬로 돌리고 적격 기업 중 최대 K개를 선정합니다.

```text
기업별: START ─┬─ clinical ─┐
               ├─ market   ─┤
               ├─ traction ─┼→ review ─(보완 요청·1회차)→ 요청받은 Agent만 재실행 → review
               └─ risk     ─┘           └─────────────→ finalize(판정) → END
전체:   START → 기업별 그래프(병렬, Send) → select → END
```

```bash
# 저장된 임상·Risk 결과를 재사용하고, 시장·실적은 새로 실행 (API 비용 발생)
python -m agents.investment_review --as-of 2026-09-30

# 저장된 시장·실적 결과까지 재사용 → 06이 보완을 요청한 Agent만 호출
python -m agents.investment_review --market-json m.json --traction-json t.json --only c001 c022
```

- 팀원 노드를 그대로 호출하고, 래퍼가 Agent별 입력 형식(`review_requests` list/dict, Risk 자체 ID)을 맞춥니다.
- 결과가 이미 있고 보완 요청이 없으면 Agent를 호출하지 않습니다. Agent 예외는 그 분석만 `failed`로 기록합니다.
- Risk 내부 보완은 끄고(`max_reviews=0`) 06 보완 루프(기업당 1회)만 씁니다.
- 기업 ID는 `inputs.py`가 02 임상과 같은 규칙(c001~)으로 만들고, Risk ID는 기업명으로 연결합니다 (01 정규화 대체).
- 02 임상 보완 결과는 `--out/clinical_results/`에 저장됩니다(`CLINICAL_RESULT_DIR`).
- 결과: `--out/final_reviews.json` (기본 `outputs/investment_review/`)

## 보고서

`report/`가 구조화 → (선택) LLM 서술 → 검증 → 렌더링 순서로 `final_report.md`를 만듭니다. 목차는 설계서 27쪽을 따릅니다.

```text
# 투자 검토 보고서
## 1. SUMMARY              전체 결과 요약 (1/2쪽 이내)
## 2. 선정 기업             기업별 고객 문제·시장 / 기술·임상·인허가 / 사업모델·실적 / 운영 리스크 + C1~C6 비교표
## 3. 전체 후보 판정        모든 후보의 판정·총점·사유·0점 처리 항목
## 4. 보완 이력·한계
## 5. 추가 실사 질문
## REFERENCE               본문에서 [n]으로 인용한 원문 출처만
```

```bash
python -m agents.investment_review.report --run-dir outputs/investment_review          # 템플릿 문장 (기본)
python -m agents.investment_review.report --run-dir outputs/investment_review --llm    # SUMMARY·핵심 검토 논점 LLM 서술
```

- 표·수치는 템플릿으로 직접 렌더링하고, LLM은 SUMMARY와 선정 기업별 핵심 검토 논점만 씁니다 (`REPORT_MODEL`, 없으면 `MARKET_MODEL`).
- `checker.py`가 분량(5쪽·SUMMARY 1/2쪽, 줄 너비 기준 추정), 인용 번호 ↔ REFERENCE 일치, 전체 후보 포함을 검사해 `report_check.json`에 남깁니다.
- LLM 문장에 보고서 데이터에 없는 숫자가 있으면 그 문장은 버리고 템플릿 문장을 씁니다. **의미 오류(예: "미발견"을 "없음 확인"으로 쓰기)는 잡지 못하므로 LLM 서술은 선택 기능입니다.**
- 전체 실행(`python -m agents.investment_review`)도 끝에 보고서를 만듭니다 (`--no-report`, `--llm-report`).

## 테스트

샘플은 유료 API 없이 02~05 실제 코드를 실행해 만듭니다. 팀원 코드가 바뀌면 다시 생성합니다.

```bash
python -m agents.investment_review.tests.fixtures.build_fixtures
```


```bash
python -m unittest discover -s agents/investment_review/tests -t . -v
```
