"""Conservative evidence screening; never assigns a risk score."""
import argparse
import json
import re
from pathlib import Path

TERMS = {
    'partnerships': ('협력', '협약', '공급', '파트너', '공동연구', 'MOU', '계약'),
    'leadership': ('대표', '임원', 'CTO', 'CEO', '창업', '선임', '퇴임', '후임'),
    'operational_events': ('중단', '종료', '사고', '분쟁', '소송', '복구', '재개', '시정'),
}
QUESTIONS = {
    'partnerships': ['핵심 외부 자원과 계약 기간·독점 조건은 무엇인가?', '협력 중단 시 대체 공급·자원 확보 수단이 있는가?'],
    'leadership': ['기준일 현재 핵심 역할 담당자는 누구인가?', '역할 공백 시 후임·직무대행·인계 체계가 있는가?'],
    'operational_events': ['현재 운영에 영향을 주는 미해결 사건이 있는가?', '발생한 사건의 영향 범위와 대응·해결 근거는 무엇인가?'],
}
CATEGORIES = dict(zip(TERMS, ['external_dependency', 'key_person_continuity', 'operational_incidents']))


def screen(source, name, as_of):
    body = source['content']
    reasons = []
    if '%PDF-' in body[:100] or ('endstream' in body and '/FlateDecode' in body):
        reasons.append('unreadable_pdf')
    normalize = lambda value: re.sub(r'\s+', '', value).casefold()
    needle, normalized = normalize(name), normalize(body)
    if needle not in normalized:
        reasons.append('company_not_found_in_saved_body')
    if source.get('published_at') and source['published_at'] > as_of:
        reasons.append('published_after_as_of')
    if len(body.strip()) < 200:
        reasons.append('insufficient_body')
    # Only nearby text, not a whole directory of unrelated companies.
    windows = []
    for match in re.finditer(re.escape(name), body, re.IGNORECASE):
        windows.append(body[max(0, match.start()-100):match.end()+400])
    nearby = ' '.join(windows)
    groups = [group for group, words in TERMS.items() if any(word.casefold() in nearby.casefold() for word in words)]
    if not groups:
        reasons.append('no_local_topic_match')
    return {'source_id': source['source_id'], 'status': 'needs_review' if reasons else 'candidate',
            'reasons': reasons, 'categories': groups,
            'date_status': 'known' if source.get('published_at') else 'unknown',
            'verification_status': 'unverified'}


def review_folder(folder, output=None):
    # Local imports avoid a module cycle when collection invokes this function.
    from .collect import save_json, save_csv
    state = json.loads((folder/'state.json').read_text())
    sources = json.loads((folder/'sources.json').read_text())
    logs = json.loads((folder/'search_log.json').read_text())
    output = output or folder/'reviewed'
    output.mkdir(parents=True, exist_ok=False)
    checked = [screen(s, state['company_profile']['company_name'], state['as_of']) for s in sources]
    by_id = {s['source_id']: s for s in sources}
    accepted = [by_id[r['source_id']] for r in checked if r['status']=='candidate']
    coverage, records = {}, {}
    for group in TERMS:
        ids = [r['source_id'] for r in checked if r['status']=='candidate' and group in r['categories']]
        relevant_logs = [log for log in logs if log['category']==CATEGORIES[group]]
        failed = any(log['status']=='search_failed' for log in relevant_logs)
        incomplete = not relevant_logs or any(log['status']=='budget_exhausted' for log in relevant_logs)
        coverage[group] = {
            'collection_status': 'search_failed' if failed else 'partial' if incomplete else 'searched',
            'evidence_status': 'unverified_candidates' if ids else 'not_confirmed',
            'source_ids': ids, 'risk_level': None,
            'limitation': '후보 문서가 있어도 사실·현재 상태 검증 전입니다.' if ids else '현재 확보한 자료로 확인하지 못했습니다. 정보가 없거나 문제가 없다는 뜻은 아닙니다.',
            'due_diligence_questions': QUESTIONS[group],
        }
        records[group] = [{'source_id': sid, 'title': by_id[sid]['title'], 'url': by_id[sid]['url'],
                          'excerpt': by_id[sid]['content'], 'verification_status': 'unverified'} for sid in ids]
        save_csv(output/f'{group}.csv', records[group], ['source_id','title','url','excerpt','verification_status'])
    save_json(output/'quality_review.json', checked)
    save_json(output/'coverage.json', coverage)
    save_json(output/'sources.json', accepted)
    save_json(output/'records.json', records)
    save_csv(output/'sources.csv', accepted, ['source_id','title','url','published_at','content'])
    state['company_evidence'] = accepted
    state['risk_collection_coverage'] = coverage
    save_json(output/'state.json', state)
    return {'reviewed': len(sources), 'candidates': len(accepted), 'needs_review': len(sources)-len(accepted), 'output': str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(review_folder(args.input_dir, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
