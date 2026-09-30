# 개발 및 실행 안내

[README로 돌아가기](../../README.md)

## 설치 및 실행

아래는 현재 구현된 모듈의 개별 실행 방법입니다. 전체 후보부터 최종 보고서까지 한 번에 실행하는 통합 진입점은 아직 없습니다. Python 3.11 이상을 권장합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Risk 분석

로컬 `.env`에 `OPENAI_API_KEY`, `RISK_MODEL`을 설정합니다. 기본 Risk 실행은 웹검색을 하지 않으므로 Tavily 키가 필요하지 않습니다.

```bash
python -m agents.risk \
  --company-id <manifest에_등록된_company_id> \
  --output outputs/risk_result.json
```

`agents/risk/data/frozen_manifest.json`과 등록된 42개 기업의 원문 입력이 필요합니다. **데이터·manifest·모델 캐시는 Git에서 제외되므로 새 환경에는 별도로 전달해야 합니다.** 파일 해시가 다르면 실행을 중단합니다.

Risk는 `로딩 → 근거 선택 → 분석 → 검토 → 조건부 보완 → 반환`으로 실행합니다. 최초 원문 최대 8개, 내부 보완 시 최대 8개를 추가하며 모델에는 최대 40개 구간을 전달합니다. 내부 보완은 최대 1회, 실행당 모델 요청은 최대 2회입니다. 캐시 적중·근거 없음은 호출하지 않습니다. 상세 입력·출력과 상위 그래프 연결은 [Risk README](risk.md)를 참고하세요.

### 실적·성장성 분석

```bash
python -m agents.traction_growth.agent --help
python -m agents.traction_growth.agent 레모넥스 --as-of 2026-09-30 --no-llm
```

`--no-llm`은 모델 호출을 끄는 옵션이며 외부 자료 조회까지 끄지는 않습니다. 수집 경로에 따라 `DART_API_KEY`, `TAVILY_API_KEY`, `DATA_GO_KR_API_KEY` 등의 설정이 필요합니다. 모델 추출을 사용할 때는 `OPENAI_API_KEY`와 선택적으로 `TRACTION_LLM_MODEL`을 설정합니다. [실행 코드](../../agents/traction_growth/agent.py), [판단 기준](traction_growth.md), [스키마](../../agents/traction_growth/schema.py)를 참고하세요.

### 테스트

```bash
python -m pytest tests agents/risk -q
```

Risk의 그래프 흐름·보완 한도·캐시·입력 해시·근거 연결·실패 처리를 가상 응답으로 확인합니다. 실제 기업 정보의 사실성 검증이나 전체 시스템 통합 테스트를 대신하지 않습니다.

## 폴더 구조

```text
README.md
agents/
  clinical_regulatory/   # 임상·인허가 Agent
  market/                # 시장·사업성 Agent
  investment_review/     # 투자 심사·보고서 Agent (main 기준)
  risk/                  # Risk Agent
    collection/          # 수집·선별 도구
    data/                # 고정 입력·manifest·캐시 (Git 제외)
  traction_growth/       # 실적·성장성 Agent
rag/market/              # 시장 RAG 문서 처리·임베딩·검색
data/                    # 후보 CSV·분석 데이터
examples/market/         # 시장 분석 입력 예시
docs/
  readme/                # README 연결 문서
    images/              # 전체·노드별 흐름도
    development.md       # 설치·실행·폴더 구조
    evaluation_policy.md # 평가 정책
    future_work.md       # 상세 개선 계획
    risk.md              # Risk 입력·출력
    traction_growth.md   # 실적·성장성 판단 기준
    output_example.md    # 최종 산출물 예시
  diagrams/              # 흐름도 원본·편집 소스
tests/                   # 시장·RAG·Risk 테스트
scripts/                 # 데이터 수집 도구
outputs/                 # 실행 결과 (Git 제외)
requirements.txt
```
