"""재수집 노드 테스트 (가짜 검색, 외부 호출 없음): python -m pytest agents/data_collect -q"""
from agents.data_collect.recollect import (
    build_collection_request,
    collect_additional,
    data_recollect_node,
    published_date,
    requests_from_unresolved,
)

PROFILE = {"company_id": "c900", "company_name": "테스트AI", "aliases": ["TestAI"]}
DATASET = {"version": "v1",
           "sources": [{"source_id": "s1", "title": "기존 기사", "url": "https://news.example.com/old",
                        "published_at": "2026-01-01", "collected_at": "2026-10-13"}],
           "records": [{"record_id": "r1", "source_id": "s1", "content": "테스트AI 기존 자료", "locator": None,
                        "tags": ["traction", "news"]}]}
REQUEST = {"request_id": "collect-01", "attempt": 1, "based_on_dataset_version": "v1", "target_workers": ["traction"],
           "items": [{"worker": "traction", "missing_id": "c900:traction:mi01", "question": "공급 계약 자료가 있는가?"}]}


def hit(url, content="테스트AI가 병원과 공급 계약을 체결했다", published="2026-06-01"):
    return {"title": "기사", "url": url, "content": content, "published_at": published}


def run(hits, request=REQUEST, dataset=DATASET):
    queries = []

    def search(query, as_of):
        queries.append(query)
        return hits
    out = collect_additional("c900", PROFILE, dataset, request, "2026-10-13", search=search)
    return out, queries


def test_adds_records_and_bumps_version():
    out, queries = run([hit("https://news.example.com/new")])
    result, ds = out["collection_result"], out["dataset"]
    assert queries == ["테스트AI 공급 계약 자료가 있는가?"]
    assert result["status"] == "updated" and result["added_records"] == 1 and result["updated_workers"] == ["traction"]
    assert ds["version"] != "v1" and result["dataset_version"] == ds["version"]
    new = ds["records"][-1]
    assert new["tags"][:2] == ["traction", "recollected"] and new["company_ids"] == ["c900"]
    assert new["source_id"] in {s["source_id"] for s in ds["sources"]}
    assert new["metadata"]["missing_ids"] == ["c900:traction:mi01"]
    assert DATASET["version"] == "v1" and len(DATASET["records"]) == 1   # 입력은 그대로


def test_filters_existing_future_and_unrelated():
    out, _ = run([hit("https://news.example.com/old"),                       # 이 Worker가 이미 가진 출처
                  hit("https://news.example.com/future", published="2026-12-01"),   # 기준일 이후
                  hit("https://news.example.com/other", content="다른 회사 이야기")])  # 기업 언급 없음
    assert out["collection_result"]["status"] == "no_new_data"
    assert out["dataset"] is DATASET   # 새 자료가 없으면 version·내용 유지


def test_existing_url_for_another_worker_reuses_source():
    request = {**REQUEST, "items": [{"worker": "risk", "missing_id": "m", "question": "분쟁 자료"}]}
    out, _ = run([hit("https://news.example.com/old")], request)
    new = out["dataset"]["records"][-1]
    assert out["collection_result"]["updated_workers"] == ["risk"]
    assert new["source_id"] == "s1" and len(out["dataset"]["sources"]) == 1   # 같은 URL은 기존 출처를 쓴다


def test_stale_request_and_search_failure():
    out, queries = run([hit("https://news.example.com/new")], {**REQUEST, "based_on_dataset_version": "v0"})
    assert out["collection_result"]["status"] == "stale_request" and queries == []

    def broken(query, as_of):
        raise RuntimeError("down")
    out = collect_additional("c900", PROFILE, DATASET, REQUEST, "2026-10-13", search=broken)
    assert out["collection_result"]["status"] == "failed"
    assert out["collection_result"]["items"][0]["status"] == "search_failed"


def test_request_from_missing_items_and_node_without_request():
    results = {"traction": {"missing_items": [
        {"missing_id": "c900:traction:mi01", "item": "매출 자료", "reason": "확인되지 않음", "required_for_assessment": True},
        {"missing_id": "c900:traction:mi02", "item": "고객 수", "reason": "자료 없음", "required_for_assessment": False}]}}
    request = build_collection_request("c900", results, "v1")
    assert request["target_workers"] == ["traction"] and [i["missing_id"] for i in request["items"]] == ["c900:traction:mi01"]
    assert build_collection_request("c900", {"risk": {"missing_items": []}}, "v1") is None
    assert data_recollect_node({"company_id": "c900", "as_of": "2026-10-13", "dataset": DATASET}) == {}


def test_published_date_formats():
    assert published_date("2026-06-01T09:00:00Z") == "2026-06-01"
    assert published_date("Tue, 31 May 2022 05:17:13 GMT") == "2022-05-31"
    assert published_date(None, "https://news.example.com/2025/08/04/244864") == "2025-08-04"
    assert published_date(None, "https://news.example.com/article") is None


def test_requests_from_unresolved_items():
    unresolved = [
        {"kind": "data_limitation", "company_id": "c900", "criterion": "bias", "workers": ["clinical", "traction"],
         "text": "테스트AI: 출처 1건", "question": "테스트AI의 판단을 뒷받침할 다른 발행처의 독립 출처를 확보할 수 있는가?"},
        {"kind": "missing_item", "company_id": "c900", "worker": "risk", "missing_id": "c900:risk:mi01",
         "item": "external_dependency", "reason": "협력 기관과 계약 관계"},
        {"kind": "missing_item", "company_id": "c901", "worker": "risk", "missing_id": "x", "item": "자료", "reason": "r"}]
    requests = requests_from_unresolved(unresolved, {"c900": "v1"})
    assert list(requests) == ["c900"]                     # 버전을 모르는 기업(c901)은 건너뛴다
    request = requests["c900"]
    assert request["based_on_dataset_version"] == "v1" and request["target_workers"] == ["clinical", "traction", "risk"]
    assert [(i["worker"], i["missing_id"]) for i in request["items"]] == [
        ("clinical", "c900:bias"), ("traction", "c900:bias"), ("risk", "c900:risk:mi01")]
    assert request["items"][2]["question"] == "협력 기관과 계약 관계"    # 코드형 item은 사유 문장을 쓴다
    out = collect_additional("c900", PROFILE, DATASET, request, "2026-10-13",
                             search=lambda q, a: [hit("https://news.example.com/new")])
    assert out["collection_result"]["status"] == "updated"
