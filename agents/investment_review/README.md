# 06 Investment Review Agent

네 분석 결과를 검증하고, 보완 요청·점수 계산·판정·선정·보고서 생성을 담당합니다. (담당: 김민솔)

## 처음 실행

### 1. 설치

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install tavily-python markdown-it-py   # 루트 requirements.txt에 없음 (02 임상 웹 검색, PDF 변환)
```

PDF는 설치된 Google Chrome을 사용합니다. Chrome이 없으면 `python -m playwright install chromium`을 실행합니다.

### 2. `.env`

| 변수 | 사용처 | 비고 |
|---|---|---|
| `OPENAI_API_KEY` | 02~05 | |
| `TAVILY_API_KEY` | 02·03·04 웹 검색 | 요금제 사용 한도를 확인합니다. 전체 실행 중 한도를 넘으면 해당 검색이 실패합니다 |
| `DART_API_KEY`, `DATA_GO_KR_API_KEY` | 04 공시·국민연금 | |
| `MFDS_*` | 02 식약처 조회 | 02 설정을 따릅니다 |
| `MARKET_MODEL` | 03 | 예: `gpt-4o-mini` |
| `RISK_MODEL` | 05 | **비어 있으면 05 보완이 실패합니다.** 예: `gpt-4.1-nano` |
| `MARKET_RAG_DATA_DIR` | 03 RAG | PDF와 `metadata.csv`가 있는 폴더. 압축을 `data/market_rag/market_rag_dataset/`에 풀었다면 그 경로로 지정합니다 |
| `REPORT_MODEL` | 06 보고서 문장 작성 | 없으면 `gpt-4.1` |

### 3. Git에 없는 데이터

| 데이터 | 위치 | 준비 |
|---|---|---|
| 05 Risk 고정 데이터 | `agents/risk/data/` (`frozen_manifest.json`, `full_*`) | 05 담당에게 받습니다 |
| 03 시장 RAG 문서 | `MARKET_RAG_DATA_DIR` | 03 담당의 `market_rag_dataset.zip` |
| 03 시장 RAG 인덱스 | `data/vectorstore/market/` | `python -m rag.market.build_index --rebuild` (BGE-M3 약 2GB 다운로드) |

02 임상 결과(`agents/clinical_regulatory/clinical_results/`)는 Git에 포함되어 있습니다.

### 4. 실행

```bash
# 기업 2곳으로 먼저 확인
python -m agents.investment_review --as-of 2026-09-30 --only c001 c022

# 전체 42개
python -m agents.investment_review --as-of 2026-09-30

# 이전 실행의 시장·실적 결과 재사용 → 06이 보완을 요청한 Agent만 호출
python -m agents.investment_review --as-of 2026-09-30 \
  --market-json outputs/investment_review/market_analysis.json \
  --traction-json outputs/investment_review/traction_analysis.json
```

- 02 임상·05 Risk는 저장된 결과를 기본으로 재사용합니다. 03 시장·04 실적은 결과를 넘기지 않으면 42개 모두 새로 실행합니다.
- 전체 실행은 보완 요청 100건 이상을 포함해 수십 분이 걸리고 OpenAI·Tavily 요금이 발생합니다.
- 기업 ID는 `c001`부터 CSV 기업명 순서입니다 (02 임상과 같은 규칙).

### 5. 결과

`outputs/investment_review/` (`--out`으로 변경)

| 파일 | 내용 |
|---|---|
| `final_report.pdf`, `final_report.md` | 투자 검토 보고서 |
| `final_reviews.json` | 기업별 판정·점수·선정 |
| `*_analysis.json` | 02~05 분석 결과 (다음 실행에서 재사용) |
| `report_check.json` | 쪽수·인용 검증 결과 |
| `clinical_results/` | 02 임상 보완 결과 |

보고서만 다시 만들 때는 02~05를 호출하지 않습니다.

```bash
python -m agents.investment_review.report --run-dir outputs/investment_review
```

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
python -m agents.investment_review.report --run-dir outputs/investment_review            # 프롬프트(LLM) 문장 (기본)
python -m agents.investment_review.report --run-dir outputs/investment_review --no-llm   # 템플릿 문장만
```

문장은 [report/prompts/writer.md](report/prompts/writer.md) 프롬프트를 따릅니다. 틀리기 쉬운 부분은 프롬프트 규칙을 코드로 구현했습니다.

| 보고서 부분 | 작성 | 비고 |
|---|---|---|
| SUMMARY (6~7문장), 평가 기준 안내문, 선정 이유, 핵심 검토 논점 | LLM (`REPORT_MODEL`, 기본 `gpt-4.1`) | 검증 실패 시 사유를 알려 1회 재요청, 그래도 실패하면 템플릿 문장 |
| 부적격·판단불가 사유 | 코드 (프롬프트 4번 규칙) | LLM은 38개 목록을 끝까지 쓰지 못하고 0점 항목 수를 잘못 셈 |
| REFERENCE | 코드 (프롬프트 5번 규칙) | LLM은 발행연도·사이트명·URL을 자주 틀림. 논문 서지는 PubMed 조회 |
| 표·수치·평가 근거 | 코드 | |

- LLM 입력에는 매출(억 원), 부적격 사유별 기업 수, 0점 처리 항목의 쉬운 이름·개수 등을 미리 계산해 넣습니다. 사실 검증 전인 운영 관찰은 넣지 않습니다.
- 검증(`checker.check_narrative`): 입력에 없는 숫자(프롬프트 기준값 100·60·2 예외), 6자리 이상 날것 숫자, 코드·변수명, 요약의 선정 기업 이름 누락.
- **숫자·코드로 잡을 수 없는 의미 오류는 남을 수 있습니다** (예: 같은 점수의 3·4위를 "공동 3위"로 씀). 제출 전 사람이 읽어 확인합니다. `gpt-4o-mini`는 이런 오류가 많아 기본값으로 쓰지 않습니다.
- **PDF**: `final_report.pdf`(A4)도 함께 만듭니다 (`report/pdf.py`, `--no-pdf`로 생략). Markdown → HTML(`markdown-it-py`) → Playwright로 Chrome 인쇄. 5쪽을 넘으면 인쇄 배율을 95%·90%로 줄여 다시 만들고, 5쪽 검사는 실제 쪽수(`pypdf`)로 합니다. 설치된 Google Chrome을 먼저 쓰고, 없으면 `python -m playwright install chromium`이 필요합니다.
- 전체 실행(`python -m agents.investment_review`)도 끝에 보고서를 만듭니다 (`--no-report`, `--no-llm-report`).

## 테스트

샘플은 유료 API 없이 02~05 실제 코드를 실행해 만듭니다. 팀원 코드가 바뀌면 다시 생성합니다.

```bash
python -m agents.investment_review.tests.fixtures.build_fixtures
```


```bash
python -m unittest discover -s agents/investment_review/tests -t . -v
```
