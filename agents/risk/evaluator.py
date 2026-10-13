"""Single-pass Risk evaluation of supplied records. No collection or disk reads."""
from copy import deepcopy
from datetime import date
import json
import os

from .evidence import CATS, PROMPT, Reply, Strict, passages, validate_response


class ReviewAnswer(Strict):
    question_id: str
    answer: str
    passage_ids: list[str]
    resolved: bool


class WorkerReply(Reply):
    review_responses: list[ReviewAnswer]


WORKER_PROMPT = PROMPT + '''
review_responses에는 review_context.questions의 각 question_id에 대한 answer, passage_ids, resolved를 반환하세요.
재검토가 아니면 빈 배열입니다. 제공 자료로 답할 수 없으면 resolved=false로 명시하세요.
해결된 답변은 근거 passage_ids가 있어야 합니다. 이전 결과나 질문은 사실 근거가 아닙니다.
전달된 검색 결과나 후보 자료가 다른 기업을 가리키면 평가에서 제외하세요.
'''


def _validate(data):
    for key in ('run_id', 'company_id', 'as_of', 'schema_version', 'criteria_version'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f'{key} is required')
    date.fromisoformat(data['as_of'])
    profile = data.get('company_profile')
    if not isinstance(profile, dict) or not (profile.get('legal_name') or profile.get('company_name')):
        raise ValueError('company_profile.legal_name is required')
    if profile.get('company_id', data['company_id']) != data['company_id']:
        raise ValueError('company_profile company mismatch')
    name = profile.get('legal_name') or profile.get('company_name')
    if not isinstance(name, str) or not name.strip(): raise ValueError('company name must be text')
    dataset = data.get('dataset')
    if not isinstance(dataset, dict) or not isinstance(dataset.get('version'), str) or not dataset['version']:
        raise ValueError('dataset.version is required')
    for key, id_key in (('sources', 'source_id'), ('records', 'record_id')):
        values = dataset.get(key)
        if not isinstance(values, list):
            raise ValueError(f'dataset.{key} must be a list')
        ids = []
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get(id_key), str) or not item[id_key]:
                raise ValueError(f'{id_key} is required')
            ids.append(item[id_key])
        if len(ids) != len(set(ids)):
            raise ValueError(f'duplicate {id_key}')
    for source in dataset['sources']:
        published = source.get('published_at')
        if published is not None:
            if not isinstance(published, str): raise ValueError('published_at must be an ISO date or null')
            date.fromisoformat(published[:10])
    source_ids = {s['source_id'] for s in dataset['sources']}
    for r in dataset['records']:
        if r.get('source_id') not in source_ids:
            raise ValueError('record references unknown source_id')
        if not isinstance(r.get('content'), (str, dict, list)):
            raise ValueError('record.content must be text or structured data')
    prev, req = data.get('previous_result'), data.get('review_request')
    if prev is not None:
        if not isinstance(prev, dict): raise ValueError('previous_result must be an object')
        for key in ('run_id', 'company_id', 'as_of', 'schema_version', 'criteria_version'):
            if prev.get(key) != data[key]: raise ValueError(f'previous_result {key} mismatch')
        if prev.get('agent') != 'risk' or type(prev.get('result_version')) is not int or prev['result_version'] < 1:
            raise ValueError('invalid previous_result agent/version')
    if req is not None:
        if not isinstance(req, dict) or not isinstance(req.get('request_id'), str) or not req['request_id']:
            raise ValueError('review_request.request_id is required')
        if not prev: raise ValueError('review requires previous_result')
        if req.get('dataset_version') != dataset['version'] or prev.get('dataset_version') != dataset['version']:
            raise ValueError('interpretation review requires the same dataset_version')
        if type(req.get('attempt')) is not int or req['attempt'] < 1:
            raise ValueError('review attempt must be a positive integer')
        qs = req.get('questions')
        if not isinstance(qs, list) or not qs: raise ValueError('review requires questions')
        ids = []
        for q in qs:
            if not isinstance(q, dict) or not isinstance(q.get('question_id'), str) or not q['question_id'] or not isinstance(q.get('question'), str) or not q['question'].strip():
                raise ValueError('question_id and question are required')
            ids.append(q['question_id'])
        if len(ids) != len(set(ids)): raise ValueError('duplicate question_id')
        targets = req.get('target_finding_ids', [])
        known = {f['finding_id'] for f in prev.get('findings', [])}
        if not isinstance(targets, list) or any(t not in known for t in targets):
            raise ValueError('unknown target_finding_ids')


def _text(content):
    if isinstance(content, str): return content
    if isinstance(content, dict):
        for key in ('content', 'text', 'raw_content', 'excerpt'):
            if isinstance(content.get(key), str) and content[key].strip(): return content[key]
    return json.dumps(content, ensure_ascii=False)


def _prepare(data):
    sources = {s['source_id']: s for s in data['dataset']['sources']}
    records = {r['record_id']: r for r in data['dataset']['records']}
    evidence = []
    for rid, record in records.items():
        # Derived model extractions and general market context are not primary Risk evidence.
        if record.get('kind') in ('model_extraction', 'model_extraction_empty', 'reference_data') or record.get('scope') == 'shared_market':
            continue
        if record.get('company_ids') and data['company_id'] not in record['company_ids']: continue
        source = sources[record['source_id']]
        evidence.append({'source_id': rid, 'title': source.get('title') or '', 'url': source.get('url'),
                         'published_at': source['published_at'][:10] if source.get('published_at') else None, 'content': _text(record['content'])})
    state = {'company_profile': {'company_id': data['company_id'],
              'company_name': data['company_profile'].get('legal_name') or data['company_profile']['company_name']},
             'as_of': data['as_of'], 'company_evidence': evidence}
    chunks = passages(state)
    for chunk in chunks:
        rid = chunk['source_id']
        chunk['record_id'] = rid
        chunk['source_id'] = records[rid]['source_id']
        chunk['locator'] = records[rid].get('locator', records[rid].get('metadata', {}).get('locator'))
    return state, chunks, records, sources


def _call(payload, model):
    if not model: raise ValueError('RISK_MODEL is required')
    from openai import OpenAI
    from openai.lib._pydantic import to_strict_json_schema
    response = OpenAI(max_retries=0, timeout=90).chat.completions.create(
        model=model, messages=[{'role': 'system', 'content': WORKER_PROMPT},
                               {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
        response_format={'type': 'json_schema', 'json_schema': {
            'name': 'RiskWorker', 'strict': True, 'schema': to_strict_json_schema(WorkerReply)}},
        max_completion_tokens=6000)
    return response.model_dump(mode='json')


def evaluate_risk(worker_input, *, caller=None, model=None):
    """WorkerInput -> WorkerResult. At most one model request; no automatic retry."""
    data = deepcopy(worker_input)
    result = {k: data.get(k) for k in ('run_id', 'company_id', 'as_of', 'schema_version', 'criteria_version')}
    dataset = data.get('dataset')
    result.update(agent='risk', dataset_version=dataset.get('version') if isinstance(dataset, dict) else None,
                  result_version=1, analysis_status='failed', data={'areas': []},
                  sources=[], evidence=[], findings=[], missing_items=[], coverage=[], review_responses=[], errors=[])
    try:
        _validate(data)
    except (ValueError, TypeError, KeyError) as exc:
        result['errors'] = [{'code': 'invalid_input', 'message': str(exc)}]
        return result
    prev, request = data.get('previous_result'), data.get('review_request')
    result['result_version'] = prev['result_version'] + 1 if prev else 1
    prefix = f"{data['company_id']}:risk:v{result['result_version']}"
    state, chunks, records, sources = _prepare(data)
    payload = {'company_id': data['company_id'], 'company_name': state['company_profile']['company_name'],
               'as_of': data['as_of'], 'dataset_version': data['dataset']['version'],
               'criteria_version': data['criteria_version'], 'passages': chunks}
    if request:
        payload['review_context'] = {**request, 'previous_areas': prev.get('data', {}).get('areas', []),
                                     'previous_findings': prev.get('findings', [])}
    raw, diagnostics, failed = None, [], False
    if chunks:
        try:
            envelope = caller(payload) if caller else _call(payload, model or os.environ.get('RISK_MODEL'))
            choice = envelope['choices'][0]
            if choice.get('finish_reason') != 'stop': raise ValueError('unfinished response')
            raw = json.loads(choice['message']['content'])
        except Exception as exc:
            # Do not put credentials or provider response bodies into State.
            result['errors'].append({'code': 'evaluation_error', 'message': type(exc).__name__})
            failed = True
        accepted, coverage, diagnostics = validate_response(raw, chunks)
        if raw is None or any(d['code'] == 'invalid_root' for d in diagnostics): failed = True
    else:
        accepted = {c: [] for c in CATS}
        coverage = {c: {'unknowns': ['관련 원문 구간 미확보'], 'due_diligence_questions': []} for c in CATS}
    if len(payload['company_name'].strip()) <= 2:
        accepted = {c: [] for c in CATS}
        diagnostics.append({'code': 'short_company_name_identity_review'})
        for c in CATS:
            coverage.setdefault(c, {'unknowns': [], 'due_diligence_questions': []})['unknowns'].append('짧은 기업명으로 기업 일치 확인 필요')
    result['errors'] += [{'code': d['code'], 'message': json.dumps(d, ensure_ascii=False)} for d in diagnostics]
    ev_by_passage, finding_passages = {}, {}
    def bind(p):
        pid = p['passage_id']
        if pid not in ev_by_passage:
            eid = f'{prefix}:ev{len(ev_by_passage)+1:03d}'
            ev_by_passage[pid] = eid
            result['evidence'].append({'evidence_id': eid, 'record_id': p['record_id'],
                'source_id': p['source_id'], 'excerpt': p['text'], 'locator': p.get('locator')})
        return ev_by_passage[pid]
    for category in CATS:
        area = {'category': category, 'observations': accepted[category],
                **coverage.get(category, {'unknowns': ['평가 실패'], 'due_diligence_questions': []})}
        result['data']['areas'].append(area)
        for o in area['observations']:
            fid = f"{prefix}:fd{len(result['findings'])+1:03d}"
            result['findings'].append({'finding_id': fid, 'category': category, 'claim': o['statement'],
                'evidence_ids': [bind(p) for p in o['citations']], 'uncertainty': o['conditional_impact'],
                'related_criterion_ids': ['C6']})
            finding_passages[fid] = set(o['passage_ids'])
        for text in area['unknowns']:
            result['missing_items'].append({'missing_id': f"{prefix}:mi{len(result['missing_items'])+1:03d}",
                'item': category, 'reason': text, 'related_criterion_ids': ['C6'], 'required_for_assessment': True})
        result['coverage'].append({'coverage_id': f"{prefix}:cv{len(result['coverage'])+1:03d}",
            'area': category, 'record_ids': sorted({p['record_id'] for p in chunks}) if not failed else [],
            'status': 'not_reviewed' if failed or not chunks else 'partial' if area['unknowns'] or diagnostics else 'reviewed',
            'note': '전달 자료 중 기업명 주변 최대 40개 구간만 검토. 외부 수집 없음.'})
    if request:
        answers = raw.get('review_responses', []) if isinstance(raw, dict) else []
        parsed = {}
        rejected_ids = set()
        expected_ids = {q['question_id'] for q in request['questions']}
        if not isinstance(answers, list): answers = []
        for answer in answers:
            try:
                a = ReviewAnswer.model_validate(answer)
                if a.question_id not in expected_ids: raise ValueError('unknown question')
                if a.question_id in parsed or a.question_id in rejected_ids:
                    parsed.pop(a.question_id, None)
                    rejected_ids.add(a.question_id)
                    raise ValueError('duplicate answer')
                parsed[a.question_id] = a
            except Exception:
                result['errors'].append({'code': 'invalid_review_response', 'message': '재검토 응답 형식 오류'})
        known = {p['passage_id']: p for p in chunks}
        for q in request['questions']:
            a = parsed.get(q['question_id'])
            valid = a is not None and all(pid in known for pid in a.passage_ids)
            resolved = bool(valid and a.resolved and a.passage_ids and a.answer.strip() and not failed)
            linked = [fid for fid, pids in finding_passages.items() if valid and pids.intersection(a.passage_ids)]
            # A supported review answer is itself a finding if no observation represents it.
            if resolved and not linked:
                fid = f"{prefix}:fd{len(result['findings'])+1:03d}"
                result['findings'].append({'finding_id': fid, 'category': 'review', 'claim': a.answer,
                    'evidence_ids': [bind(known[pid]) for pid in dict.fromkeys(a.passage_ids)],
                    'uncertainty': '원문 연결이며 사실성 검증은 아님', 'related_criterion_ids': ['C6']})
                linked = [fid]
            result['review_responses'].append({'request_id': request['request_id'], 'question_id': q['question_id'],
                'answer': a.answer if valid and not failed else '제공 자료로 재검토 답변을 확인하지 못했습니다.',
                'finding_ids': linked if resolved else [], 'resolved': resolved})
            if not resolved:
                result['missing_items'].append({'missing_id': f"{prefix}:mi{len(result['missing_items'])+1:03d}",
                    'item': q['question'], 'reason': '재검토 질문 미해결', 'related_criterion_ids': ['C6'],
                    'required_for_assessment': True})
    used_sources = {p['source_id'] for p in chunks}
    result['sources'] = [deepcopy(s) for sid, s in sources.items() if sid in used_sources]
    result['data']['limitation'] = '원문 연결은 사실 검증이 아닙니다. 자료 부족은 위험 없음이 아닙니다.'
    result['analysis_status'] = 'failed' if failed else 'partial' if result['errors'] or result['missing_items'] or not chunks else 'complete'
    return result
