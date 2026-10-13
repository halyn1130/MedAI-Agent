# 데이터 준비 (수집·로딩)

평가 Worker(clinical · market · traction · risk)는 자료를 수집하지 않습니다.
이 폴더가 Orchestrator에 넘길 기업별 입력(`company_profile`, `dataset`)을 준비합니다.

## 실행

```bash
python -m agents.data_collect                               # 기업별 입력 준비 → outputs/bundles/
python -m agents.orchestrator --input outputs/bundles       # Orchestrator 실행
```

`python -m agents.data_collect`는 아래 순서로 동작합니다.

1. **취합 데이터셋(`outputs/collected_dataset.json`)이 있으면** 그 파일에서 기업별 입력을 꺼냅니다. 외부 조회를 하지 않습니다.
2. **없으면** 기업 목록(`data/startup_list.csv`)을 읽어 새로 수집합니다.

| 옵션 | 내용 |
|---|---|
| `--collected <경로>` | 취합 데이터셋 위치 (기본 `outputs/collected_dataset.json`) |
| `--input <경로>` | 기업 목록 CSV (기본 `data/startup_list.csv`) |
| `--out <폴더>` | 저장 폴더 (기본 `outputs/bundles`) |
| `--names A,B` · `--limit N` | 일부 기업만 |
| `--recollect` | 취합 데이터셋이 있어도 CSV에서 새로 수집 |
| `--only clinical,risk` · `--force` · `--concurrency N` · `--as-of` | 새로 수집할 때만 적용 |

기업별 파일: `{company_id, as_of, company_profile, dataset{version, sources, records}}`

## 파일

| 파일 | 역할 | 외부 조회 |
|---|---|---|
| `__main__.py` | 위 실행부. 취합 데이터셋이 있으면 `load`, 없으면 `collect` | — |
| `load.py` | 취합 데이터셋 한 파일 → 기업별 입력 | 없음 |
| `collect.py` | 분야별 수집 코드를 불러 기업 1곳의 데이터셋을 새로 수집 | 있음 |
| `update.py` | 취합 데이터셋에서 한 분야의 레코드를 새 수집 결과로 교체, 기업 ID 변경 | 없음 |

### 취합 데이터셋에서 꺼낼 때 (`load.py`)

- **넣는 레코드**: 그 기업에 확정 배정된 레코드(`record_ids`)와 공통 자료(`shared_record_ids`).
  기업 일치가 확인되지 않은 후보와 대체된(superseded) 레코드는 넣지 않습니다.
- **배분 표시**: tags가 없는 레코드에는 `domain` 값을 tags로 붙입니다. Orchestrator와 Worker가 tags로 자료를 고릅니다.
- **company_profile**: 입력 CSV 행에 `clinical_profile`(정식명·제품·회사 주장 원문)을 덮어씁니다.
  임상 Worker가 제품 ID와 회사 주장을 여기서 읽습니다.

### 새로 수집할 때 (`collect.py`)

| 분야 | 불러오는 코드 | 방식 |
|---|---|---|
| clinical | `clinical_regulatory.collection.collect_clinical_data` | 식약처·openFDA·PubMed·웹 검색 (제품·주장 추출에 LLM 사용) |
| traction | `traction_growth.collect.collect_dataset` | OpenDART·뉴스·국민연금 |
| risk | `risk.collection.collect.collect_company` + 품질 선별 | Tavily 검색 |
| market | `data/market_evaluation/dataset.json` | 산업 공통 고정 자료 (기업별 수집 없음) |

- clinical을 먼저 실행해 보강된 프로필(정식명·영문명)을 다른 분야에 넘깁니다.
- 한 분야가 실패해도 나머지는 계속 수집하고, 실패 내용은 `collection_log`에 남깁니다.
- 모든 레코드의 tags에 Worker 이름이 들어갑니다.
- 필요한 키(`.env`): `TAVILY_API_KEY`, `MFDS_API_KEY`, `DART_API_KEY`, `DATA_GO_KR_API_KEY`, LLM 키

### 취합 데이터셋을 고칠 때 (`update.py`)

```bash
python -m agents.data_collect.update --input collected_dataset.json --output outputs/collected_dataset.json \
  --domain traction --datasets outputs/traction_datasets --as-of 2026-10-13 \
  --provenance "traction_cache_2026-10-13_v2.1.zip → traction_growth.collect.collect_dataset" \
  --rename company_d178040092e7=c042
```

입력 파일은 고치지 않고 새 파일을 씁니다. 변경 내용은 파일의 `revisions`에 기록됩니다.

## 현재 취합 데이터셋 (`collected-0398548d442651bb`)

- 기업 42개. 임상·실적은 2026-10-13, 리스크는 2026-09-30 기준, 시장은 공통 자료 193건.
- c021(상호기입)은 실적·리스크 자료가 없어 임상·시장만 실행됩니다.
- 35MB라 Git에 올리지 않습니다(`outputs/`는 Git 제외). 팀 드라이브로 공유합니다.

## 남은 과제

- **재수집**: 데이터 부족 시 `collection_request`의 질문으로 추가 수집하는 경로는 아직 없습니다.
- **리스크 자료 갱신**: 리스크는 9월 30일 수집분입니다.
