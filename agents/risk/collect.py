"""Collect candidate public evidence: python -m agents.risk.collect --help."""
import argparse
import csv
import json
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from .providers import ProviderError, TavilySearch

QUERIES = {
    'partnerships': ['업무협약', '공급 계약', '협력 종료 대체'],
    'leadership': ['경영진 대표', '임원 선임 퇴임', '후임 업무 인계'],
    'operational_events': ['서비스 중단', '사고 분쟁', '복구 재개'],
}
CATEGORIES = dict(zip(QUERIES, ['external_dependency', 'key_person_continuity', 'operational_incidents']))


def load_companies(path):
    if path.suffix.lower() == '.json':
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        rows = value if isinstance(value, list) else [value]
    else:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
    output, seen = [], {}
    for row in rows:
        profile = row.get('company_profile', row)
        name = next((str(profile[k]).strip() for k in ('company_name', 'company', '기업명', '회사명') if profile.get(k)), '')
        if not name:
            raise ValueError('Input needs company_name, company, 기업명 or 회사명')
        cid = str(profile.get('company_id') or 'company_' + sha256(name.encode()).hexdigest()[:12])
        if cid in seen:
            if seen[cid]['company_name'] != name:
                raise ValueError('Same company_id has conflicting company names')
            seen[cid]['input_records'].append(profile)
            continue
        item = {**profile, 'company_id': cid, 'company_name': name, 'input_records': [profile]}
        seen[cid] = item
        output.append(item)
    return output


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def save_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            values = {}
            for key in fields:
                value = row.get(key)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                # Spreadsheet formula protection. JSON preserves the original text.
                if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                    value = "'" + value
                values[key] = value
            writer.writerow(values)


def collect_company(profile, search, folder, as_of, max_calls=9):
    folder.mkdir(parents=True, exist_ok=False)
    logs, sources, rows = [], {}, {key: [] for key in QUERIES}
    pairs = set()
    calls = 0
    # Round robin: each category gets a query before follow-up queries consume budget.
    for index in range(3):
        for group, terms in QUERIES.items():
            query = f'"{profile["company_name"]}" {terms[index]}'
            log = {'category': CATEGORIES[group], 'query': query, 'checked_at': date.today().isoformat(), 'source_ids': []}
            if calls >= max_calls:
                logs.append({**log, 'status': 'budget_exhausted'})
                continue
            calls += 1
            try:
                results = search.search(query, company_id=profile['company_id'], as_of=as_of)
            except ProviderError:
                logs.append({**log, 'status': 'search_failed', 'error': 'Search provider failed'})
                continue
            future = 0
            for source in results:
                if source.published_at and source.published_at > as_of:
                    future += 1
                    continue
                # Deduplicate identical URL/content; changed content remains a separate source.
                key = sha256((source.url + '\n' + source.content).encode()).hexdigest()
                if key not in sources:
                    sources[key] = source.model_dump(mode='json')
                record = sources[key]
                sid = record['source_id']
                if sid not in log['source_ids']:
                    log['source_ids'].append(sid)
                if (group, sid) in pairs:
                    continue
                pairs.add((group, sid))
                rows[group].append({
                    'record_id': group + '_' + key[:16], 'company_id': profile['company_id'],
                    'company_name': profile['company_name'], 'category': CATEGORIES[group],
                    'source_ids': [sid], 'title': source.title, 'url': source.url,
                    'published_at': source.published_at, 'checked_at': source.checked_at,
                    'as_of': as_of, 'excerpt': source.content, 'verification_status': 'unverified',
                    'current_status': 'unknown', 'event_date': None, 'product_id': None,
                    'missing_items': ['기업·제품 일치 확인', '원문 사실 추출', '현재 상태 및 대응 확인'],
                })
            logs.append({**log, 'status': 'success', 'excluded_future_sources': future})
            # Checkpoint each completed search, so interrupted runs retain evidence.
            save_json(folder / 'checkpoint.json', {'sources': list(sources.values()), 'records': rows, 'search_log': logs})
    fields = ['record_id', 'company_id', 'company_name', 'category', 'product_id', 'source_ids', 'title', 'url', 'published_at', 'checked_at', 'as_of', 'event_date', 'excerpt', 'verification_status', 'current_status', 'missing_items']
    for group in QUERIES:
        save_csv(folder / f'{group}.csv', rows[group], fields)
    evidence = list(sources.values())
    save_csv(folder / 'sources.csv', evidence, ['source_id', 'title', 'url', 'publisher', 'source_type', 'published_at', 'checked_at', 'content'])
    save_json(folder / 'sources.json', evidence)
    save_json(folder / 'records.json', rows)
    save_json(folder / 'search_log.json', logs)
    save_json(folder / 'state.json', {'as_of': as_of, 'company_profile': profile, 'company_evidence': evidence, 'review_requests': {}, 'retry_counts': {}})
    manifest = {'company_id': profile['company_id'], 'as_of': as_of, 'search_calls': calls,
                'source_count': len(evidence), 'failed_calls': sum(x['status'] == 'search_failed' for x in logs),
                'coverage': {key: len(value) for key, value in rows.items()},
                'limitations': ['검색 후보 자료이며 사실 검증 전입니다.', '발행일 미상은 기준일 당시 존재 미확인입니다.', '검색 API 반환 텍스트는 원문 전체가 아닐 수 있으며 최대 12000자로 제한됩니다.', '모든 공개 자료의 수집을 보장하지 않습니다.']}
    save_json(folder / 'manifest.json', manifest)
    from .quality import review_folder
    manifest['quality_review'] = review_folder(folder)
    save_json(folder / 'manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Company CSV or State JSON')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).parent / 'data')
    parser.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    parser.add_argument('--limit', type=int, default=1, help='Maximum companies (default: 1)')
    parser.add_argument('--max-search-calls', type=int, default=9, help='Per company')
    parser.add_argument('--dry-run', action='store_true', help='Show query plan without API calls or file writes')
    args = parser.parse_args()
    if args.limit < 1 or not 0 <= args.max_search_calls <= 30:
        parser.error('limit must be positive; max-search-calls must be 0..30')
    profiles = load_companies(args.input)[:args.limit]
    if args.dry_run:
        print(json.dumps({'company_count': len(profiles), 'max_total_calls': len(profiles) * min(9, args.max_search_calls), 'companies': [{'company_name': p['company_name'], 'queries': [f'"{p["company_name"]}" {QUERIES[g][i]}' for i in range(3) for g in QUERIES][:args.max_search_calls]} for p in profiles]}, ensure_ascii=False, indent=2))
        return
    from dotenv import load_dotenv
    load_dotenv()
    search = TavilySearch()
    run = args.output_dir / datetime.now().strftime('run_%Y%m%d_%H%M%S_%f')
    manifests = []
    for profile in profiles:
        # Never use untrusted input IDs as filesystem paths.
        folder = run / sha256(profile['company_id'].encode()).hexdigest()[:16]
        manifests.append(collect_company(profile, search, folder, args.as_of, args.max_search_calls))
        print(f'Collected {len(manifests)}/{len(profiles)}: {folder}')
    run.mkdir(parents=True, exist_ok=True)
    save_json(run / 'manifest.json', manifests)
    print(f'Done: {run}')


if __name__ == '__main__':
    main()
