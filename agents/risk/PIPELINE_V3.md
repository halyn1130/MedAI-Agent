# Risk 수집·분석 v3 (이전 실행 방식)

현재 기본 Agent는 고정 데이터 기반 LangGraph입니다. [현재 실행 안내](README.md)를 먼저 참고하세요. 아래 전체 수집 도구는 데이터셋을 갱신할 때만 별도로 실행합니다.

## 실행

```bash
# 전체 CSV: 새 Tavily 수집 → 선별 → 기업당 GPT 1회 → 검증·Markdown
python -m agents.risk.run_all --input data/startup_list.csv

# 이미 수집한 기업: GPT 캐시 자동 재사용
python -m agents.risk --company-id <고정_manifest의_company_id> --output 결과.json

# 기존 전체 수집: 재검색 없이 분석
python -m agents.risk.batch_analyze --input-dir 수집폴더

# 실행 중단 뒤 동일 수집 폴더에서 재개 (완료 기업 재수집 없음)
python -m agents.risk.run_all --run-dir agents/risk/data/full_실행시간
```

## 비용과 캐시

- 기업당 세 영역을 하나의 요청으로 처리한다. OpenAI SDK 재시도는 0회이며 출력 수정 재호출도 없다.
- 원문 구간이 없으면 GPT를 호출하지 않는다.
- 원본 응답은 `data/model_cache/`에 검증 전에 저장한다. 모델·프롬프트·스키마·기업·기준일·구간 내용이 같으면 같은 캐시를 사용한다.
- 검증 규칙이나 Markdown 작성 코드만 수정하면 기존 응답으로 재검증할 수 있다. 모델·프롬프트·입력 변경은 새로운 요청이 된다.
- API 실패도 캐시한다. 자동 재시도하지 않는다. 요청 도중 중단돼 `.pending`이 남으면 자동 재호출을 막는다. 재시도하려면 해당 요청 상태와 비용을 먼저 확인한다.
- Tavily는 기업당 최대 9회이며 새 실행 폴더를 만들면 다시 수집한다. 부분 수집 폴더는 자동으로 다시 호출하지 않고 오류로 표시한다.

## 출력 계약 변경

기본 CLI와 batch_analyze는 `company_analysis.py`의 `risk-company-3` 계약을 사용한다.
기존 `agent.py`와 `providers.ChatAnalyzer`의 영역별 노드는 이전 계약·테스트 호환용이며 새 실행 경로에서 호출하지 않는다.
LangGraph에서 새 계약을 사용하려면 `analyze(state, cache_dir)` 결과를 `risk_analysis` 키로 연결하고 소비 노드도 v3 계약에 맞춰야 한다. 기존 보완 요청 루프는 v3에 아직 연결되지 않았다.

기업 결과의 `areas`는 세 영역을 포함하며, 각 영역에는 다음 값이 있다.

- `observations`: 공개 자료에서 추출한 관측/주장과 근거 구간, 조건부 영향
- `unknowns`: 미확인 사항
- `due_diligence_questions`: 실사 질문

`kind=observation`은 협력 관계·경영진 같은 기초 사실 후보, `risk_signal`은 실제 사건/변화가 있는 위험 신호 후보다. 모두 `source_linked_not_fact_verified`로 표시하며 의미 검토가 필요하다.

## 검증

원문 구간은 Python이 기업명 주변에서 자르고 정확한 위치·본문·출처를 저장한다. GPT는 구간 ID만 선택한다. 존재하지 않는 ID, 빈 근거, 정보 부재를 사실로 쓴 항목은 보류하고 다른 정상 항목은 보존한다. 결과 JSON의 `diagnostics`와 캐시 원본에서 다시 검토할 수 있다.

`complete`는 실행 완료다. 인용 구간이 실제 주장을 지지하는지, 다른 기업의 인물은 아닌지, 사건이 현재 유효한지는 별도 의미 검토가 필요하다. 짧은 기업명·동명이인·포트폴리오 목록은 오탐이 생길 수 있다. 필터에 탈락하거나 자료를 찾지 못했다고 위험 없음으로 처리하지 않는다.

기존 README의 영역별 3회 호출·자동 수정 1회 설명은 이전 경로에만 해당한다.
