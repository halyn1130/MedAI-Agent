# Risk 데이터 수집

기업 목록을 읽어 외부 협력 관계, 경영진, 운영 관련 사건의 공개 자료를 검색하고 분석 입력을 만듭니다.

```text
collection/
├── collect.py          # 기업 목록 로딩·검색·CSV/JSON 저장
├── search.py           # Tavily 검색 API
├── quality.py          # 기업 일치·발행일·본문 품질 선별
├── additional_data.md  # 수집 항목과 기준
├── test_collect.py     # 수집 테스트
└── test_quality.py     # 선별 테스트
```

프로젝트 루트에서 실행합니다.

```bash
# 검색 계획만 확인 (API 호출 없음)
python -m agents.risk.collection --input data/startup_list.csv --limit 1 --dry-run

# 수집 및 선별 (.env의 TAVILY_API_KEY 필요)
python -m agents.risk.collection --input data/startup_list.csv --limit 1

# 기존 수집 자료를 별도 폴더에 다시 선별
python -m agents.risk.collection.quality --input-dir <기업폴더> --output-dir <새선별폴더>
```

기본 저장 위치는 기존과 같은 `agents/risk/data/run_*/`입니다. `--output-dir`로 변경할 수 있습니다. 수집 단계에서는 GPT를 호출하지 않습니다. 선별 통과는 사실 확인이나 리스크 부재를 의미하지 않습니다.

수집과 GPT 분석을 모두 실행하는 기존 `python -m agents.risk.run_all` 명령은 유지합니다.

```bash
python -m unittest agents.risk.collection.test_collect agents.risk.collection.test_quality
```
