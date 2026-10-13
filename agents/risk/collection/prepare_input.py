"""Explicit frozen-data loading step outside the Risk worker; no model calls.

python -m agents.risk.collection.prepare_input --company-id ID --run-id RUN --output input.json
"""
import argparse
import json
from pathlib import Path
from ..dataset import DEFAULT_MANIFEST, load_company


def prepare_worker_input(company_id, *, run_id, manifest_path=DEFAULT_MANIFEST,
                         schema_version='2.0', criteria_version='proposed-2.0', output_company_id=None):
    state, digest = load_company(manifest_path, company_id)
    cid = output_company_id or company_id
    profile = dict(state['company_profile'])
    profile.update(company_id=cid, legal_name=profile.get('legal_name') or profile.get('company_name'))
    sources, records = {}, []
    for i, item in enumerate(state.get('company_evidence', [])):
        sid = item['source_id']
        source = {k: item.get(k) for k in ('source_id', 'title', 'url', 'published_at')}
        source['collected_at'] = item.get('checked_at')
        if sid in sources and sources[sid] != source: raise ValueError('Conflicting source_id')
        sources[sid] = source
        records.append({'record_id': f'{cid}:risk:input:{digest[:12]}:{i+1}', 'source_id': sid,
                        'content': item['content'], 'locator': None, 'tags': ['risk'], 'company_ids': [cid]})
    return {'run_id': run_id, 'company_id': cid, 'as_of': state['as_of'],
            'schema_version': schema_version, 'criteria_version': criteria_version,
            'company_profile': profile,
            'dataset': {'version': digest, 'sources': list(sources.values()), 'records': records},
            'previous_result': None, 'review_request': None}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--company-id', required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = prepare_worker_input(args.company_id, run_id=args.run_id, manifest_path=args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__': main()
