# 개발 및 실행 안내

[README로 돌아가기](../../README.md)

## 설치 및 실행

- 환경: Python 3.11 이상 권장, macOS·Linux 셸
- 명령 실행 위치: 저장소 루트
- 기준: main 브랜치 (`investment_review` 포함)
- 입력·정규화: 투자 심사 진입점의 CSV 로딩 단계, 별도 Agent CLI 없음

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env

# 임상 검색·보고서 생성 추가 의존성
python -m pip install tavily-python markdown-it-py

# PDF 생성용 브라우저: 설치된 Chrome이 없는 환경
python -m playwright install chromium
```

### 환경 설정

`.env`에 사용할 모듈의 키·모델 설정.

| 모듈 | 설정 |
|---|---|
| 임상·인허가 | `OPENAI_API_KEY`, `TAVILY_API_KEY`; 선택: `LLM_MODEL`, `NCBI_API_KEY` |
| 시장·사업성 | `OPENAI_API_KEY`, `TAVILY_API_KEY`, `MARKET_MODEL` |
| 실적·성장성 | 수집 경로별 `DART_API_KEY`, `TAVILY_API_KEY`, `DATA_GO_KR_API_KEY`; LLM 사용 시 `OPENAI_API_KEY`, 선택: `TRACTION_LLM_MODEL` |
| Risk | `OPENAI_API_KEY`, `RISK_MODEL` |
| 투자 심사 | 실행 대상 Agent의 설정·입력 데이터, 보고서 LLM 사용 시 `OPENAI_API_KEY` |

### 02 · 임상·인허가 분석

```bash
# CSV 앞 3개 기업 분석
CLINICAL_RESULT_DIR=agents/clinical_regulatory/clinical_results \
python -m agents.clinical_regulatory.agent data/startup_list.csv 3

# 기업명 지정
CLINICAL_RESULT_DIR=agents/clinical_regulatory/clinical_results \
python -m agents.clinical_regulatory.agent data/startup_list.csv 1 레모넥스
```

- 입력: CSV 경로 → 최대 기업 수 → 기업명 필터(선택, 쉼표 구분)
- 출력: `clinical_analysis.json`, `CLINICAL_RESULT_DIR`의 기업별 JSON
- 재실행: 기존 결과 재사용, 기업명 지정 시 해당 기업 재분석
- 전체 재분석: 명령 앞 `RERUN=1` 추가
- 식약처 API 사용: `MFDS_API_KEY` 및 `MFDS_PERMIT_*`·`MFDS_ITEM_*` 조회 설정 필요
- 기준일: 단독 CLI는 실행일 사용, `--as-of` 옵션 없음

### 03 · 시장·사업성 분석

```bash
# RAG 인덱스 구축: data/market_rag/에 PDF 배치 후
python -m rag.market.build_index

# 분석
python -m agents.market \
  --input examples/market/input.json \
  --output outputs/market_result.json

# 외부 호출 없는 동작 확인
python -m agents.market \
  --input examples/market/input.json \
  --output outputs/market_demo.json --demo
```

- 입력: 기업 프로필 또는 `company_profile` 포함 JSON, 기준일은 입력의 `as_of`
- RAG: PDF 합계 기본 200쪽 이내, BGE-M3 모델 최초 다운로드 필요
- 저장소: `data/vectorstore/market/`의 ChromaDB
- 인덱스 재구축: `python -m rag.market.build_index --rebuild`
- 인덱스 경로 변경: 셸 환경변수 `MARKET_RAG_DATA_DIR`, `MARKET_RAG_VECTORSTORE_DIR` 설정 (인덱스 CLI의 `.env` 자동 로딩 없음)
- `--demo`: 웹검색·벡터 검색·LLM 호출 비활성화, 미확인 결과 반환

### 04 · 실적·성장성 분석

```bash
# LLM 추출 없이 실행
python -m agents.traction_growth.agent 레모넥스 --as-of 2026-09-30 --no-llm

# LLM 추출 포함
python -m agents.traction_growth.agent 레모넥스 --as-of 2026-09-30

# 전체 후보 분석
python -m agents.traction_growth.agent --all --as-of 2026-09-30 --no-llm
```

- 입력: `data/startup_list.csv`의 기업명, 기준일
- `--no-llm`: 모델 추출만 생략, 외부 자료 조회 유지
- `--refresh`: 기존 캐시 무시·갱신
- 출력 위치: 실행 종료 시 `전체 결과` 또는 `요약` 경로 표시
- [판단 기준](traction_growth.md)

### 05 · Risk 분석

```bash
# 등록된_company_id를 manifest의 실제 ID로 변경
python -m agents.risk \
  --company-id "등록된_company_id" \
  --output outputs/risk_result.json
```

- 필수 입력: `agents/risk/data/frozen_manifest.json` 및 등록 원문
- 데이터·manifest·모델 캐시: Git 제외, 별도 확보 필요
- `--manifest`: 기본 manifest 경로 변경
- 입력 파일 해시 불일치: 실행 중단
- 외부 재검색 없음, 내부 보완 최대 1회·모델 요청 최대 2회
- [입력·출력 상세](risk.md)

### 06 · 투자 심사·보고서

```bash
# 전체 후보 분석·심사·보고서 생성
python -m agents.investment_review --as-of 2026-09-30

# 앞 3개 기업만 실행
python -m agents.investment_review --as-of 2026-09-30 --limit 3

# 저장된 결과로 보고서만 재생성
python -m agents.investment_review.report --run-dir outputs/investment_review
```

- 전제: 각 분석 Agent의 설정·입력 준비
- 저장된 임상·Risk 결과 재사용, 결과 미보유 Agent 및 심사 보완 시 실제 분석 호출
- 입력 재사용: `--clinical-dir`, `--risk-run-dir`, `--market-json`, `--traction-json`
- 대상 지정: `--only c001 c022`
- 정책: 기본 `zero_fill`, 선택 `--policy strict`·`--policy partial`
- 출력 경로: 기본 `outputs/investment_review/`, 변경 `--out`
- 산출물: `final_reviews.json`, Agent별 JSON, `run_meta.json`, `final_report.md`, `final_report.pdf`, `report_check.json`
- `--no-report`: 보고서 생성 생략
- 보고서 LLM 작성: 기본 활성화, 통합 실행에서 `--no-llm-report`로 비활성화
- 보고서 단독 실행: `--no-llm`으로 LLM 작성 비활성화, `--no-pdf`로 Markdown만 생성
- PDF 생성 실패: Markdown 보존, `report_check.json`의 `pdf_error` 확인

### 테스트

```bash
# 시장·RAG·Risk
python -m pytest tests agents/risk -q

# 투자 심사·보고서 (main 기준)
python -m pytest agents/investment_review/tests -q
```

- 검증 범위: 모듈별 로직·계약·실패 처리
- 실제 기업 정보의 사실성·전체 실서비스 실행 검증 별도

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
    final_report.pdf     # 최종 산출물 예시
    output_example.md    # 이전 예시 안내
  diagrams/              # 흐름도 원본·편집 소스
tests/                   # 시장·RAG·Risk 테스트
scripts/                 # 데이터 수집 도구
outputs/                 # 실행 결과 (Git 제외)
requirements.txt
```
