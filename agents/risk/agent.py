"""Single risk node; dependency-injected search and structured analyzer."""
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from .providers import ProviderError
from .schema import (CATEGORIES, CategoryResult, Coverage, QuestionResult,
                     ReviewRequest, ReviewResponse, RiskAgentOutput, RiskAnalysis,
                     SearchLog, Source)

QUERIES = {
    'external_dependency': '데이터 공급 기술 제휴 협력 종료 대체 공급',
    'key_person_continuity': '대표 CTO 임원 퇴임 후임 선임 역할 이관',
    'operational_incidents': '서비스 중단 복구 사고 분쟁 후속 공식 공지',
}


def merge_by_agent(left, right):
    """Use as an Annotated reducer for review_results / retry_counts."""
    return {**(left or {}), **(right or {})}


def merge_references(left, right):
    result = {s['source_id']: s for s in (left or [])}
    for source in right or []:
        sid = source['source_id']
        if sid in result and result[sid] != source:
            raise ValueError(f'Conflicting reference ID: {sid}')
        result[sid] = source
    return list(result.values())


class RiskAgent:
    def __init__(self, search, analyzer, *, max_search_calls=6, max_reviews=1):
        if max_search_calls < 0 or max_reviews < 0:
            raise ValueError('Limits must be nonnegative')
        self.search, self.analyzer = search, analyzer
        self.max_search_calls, self.max_reviews = max_search_calls, max_reviews
        self.prompt = Path(__file__).with_name('prompt.md').read_text()

    def run(self, profile, *, sources=(), previous=None, review_request=None,
            retry_count=0, as_of=None):
        company_id = profile.get('company_id')
        company_name = profile.get('company_name')
        if not company_id or not company_name:
            raise ValueError('company_profile requires company_id and company_name')
        today = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or date.today()
        prior = RiskAnalysis.model_validate(previous) if previous is not None else None
        req = ReviewRequest.model_validate(review_request) if review_request is not None else None
        if prior and prior.company_id != company_id:
            raise ValueError('Previous result belongs to another company')
        if req:
            if not prior or req.company_id != company_id:
                raise ValueError('Review requires matching previous analysis and company')
            if req.attempt != retry_count + 1 or req.attempt > self.max_reviews:
                raise ValueError('Stale request or review limit exceeded')
            known = {f.finding_id: f for f in prior.findings}
            if not set(req.finding_ids) <= known.keys():
                raise ValueError('Unknown requested finding ID')
            if any(known[i].category not in req.categories for i in req.finding_ids):
                raise ValueError('Requested finding/category mismatch')
        categories = req.categories if req else list(CATEGORIES)
        budget = min(self.max_search_calls, req.max_search_calls) if req else self.max_search_calls
        registry = {s.source_id: s for s in prior.sources} if prior else {}
        for source in sources:
            s = Source.model_validate(source)
            if s.source_id in registry and registry[s.source_id] != s:
                raise ValueError('Conflicting source ID')
            registry[s.source_id] = s
        old_source_ids = {s.source_id for s in prior.sources} if prior else set()
        findings = {f.finding_id: f for f in prior.findings} if prior else {}
        coverage = {c.category: c for c in prior.coverage} if prior else {}
        logs = list(prior.search_log) if prior else []
        seen_queries = {l.query for l in logs if l.status == 'success'}
        calls = 0
        failures = []
        diagnostics = []
        successful_categories = 0
        question_answers = {}
        for category in categories:
            targets = [f for f in findings.values() if f.category == category
                       and (not req or not req.finding_ids or f.finding_id in req.finding_ids)]
            queries = ([f'"{company_name}" {q.question}' for q in req.questions] if req
                       else [f'"{company_name}" {QUERIES[category]}'])
            local_logs = []
            for query in queries:
                if calls >= budget or query in seen_queries:
                    log = SearchLog(category=category, query=query, searched_at=today,
                        status='skipped', skip_reason='disabled' if budget == 0 else 'duplicate' if query in seen_queries else 'budget_exhausted',
                        error=None)
                else:
                    calls += 1
                    try:
                        found = self.search.search(query, company_id=company_id, as_of=today, request=req)
                        ids = []
                        for item in found:
                            s = Source.model_validate(item)
                            if s.published_at and s.published_at > today:
                                continue
                            if s.source_id in registry and registry[s.source_id] != s:
                                raise ValueError('Conflicting source ID from search')
                            # Reuse identical URL/content rather than count it as new evidence.
                            duplicate = next((x for x in registry.values()
                                if x.url == s.url and x.content == s.content), None)
                            if duplicate:
                                ids.append(duplicate.source_id)
                            else:
                                registry[s.source_id] = s
                                ids.append(s.source_id)
                        log = SearchLog(category=category, query=query, searched_at=today,
                            status='success', relevant_source_ids=ids)
                        seen_queries.add(query)
                    except (ProviderError, ValidationError, ValueError) as exc:
                        from .diagnostics import safe_diagnostic
                        diagnostics.append({'category': category, **safe_diagnostic(exc, 'search')})
                        log = SearchLog(category=category, query=query, searched_at=today,
                            status='failed', error='Search failed or returned invalid evidence')
                        failures.append(log.error)
                logs.append(log)
                local_logs.append(log)
            context = {
                'category': category, 'as_of': today, 'profile': profile,
                'search_mode': 'provided_sources_only' if budget == 0 else 'web_search',
                'sources': [s.model_dump(mode='json') for s in registry.values()
                            if not s.published_at or s.published_at <= today],
                'previous_findings': [f.model_dump(mode='json') for f in targets],
                'target_finding_ids': [f.finding_id for f in targets] if req and req.finding_ids else [],
                'review_request': req.model_dump(mode='json') if req else None,
                'search_log': [l.model_dump(mode='json') for l in logs],
            }
            try:
                result = CategoryResult.model_validate(self.analyzer.analyze(self.prompt, context))
                self._validate_result(result, category, registry, targets, req, today)
                for f in result.findings:
                    if f.finding_id in findings and findings[f.finding_id].category != category:
                        raise ValueError('Finding ID belongs to another category')
                # Commit only after the full patch passes validation.
                if not req or not req.finding_ids:
                    findings = {i: f for i, f in findings.items() if f.category != category}
                findings.update({f.finding_id: f for f in result.findings})
                cov = result.coverage.model_copy(deep=True)
                cov.finding_ids = [i for i, f in findings.items() if f.category == category]
                if any(l.status == 'failed' for l in local_logs):
                    cov.limitation = (cov.limitation or '') + ' 추가 웹검색 실패; 조사 범위 제한.'
                    if not cov.finding_ids:
                        cov.status = 'search_failed'
                elif any(l.status == 'skipped' for l in local_logs):
                    cov.limitation = (cov.limitation or '') + (' 추가 검색 비활성화: 제공 자료만 검토.' if budget == 0 else ' 검색 생략: 한도 또는 이전 검색 중복.')
                    if cov.status == 'no_relevant_evidence_found':
                        cov.status = 'insufficient_information'
                coverage[category] = cov
                for answer in result.question_results:
                    # Multi-category reviews may yield several partial answers.
                    old = question_answers.get(answer.question_id)
                    rank = {'unanswered': 0, 'partially_answered': 1, 'answered': 2}
                    if old is None or rank[answer.status] > rank[old.status]:
                        question_answers[answer.question_id] = answer
                successful_categories += 1
            except (ProviderError, ValidationError, ValueError) as exc:
                from .diagnostics import safe_diagnostic
                detail = getattr(exc, 'diagnostic', None) or safe_diagnostic(exc, 'result_validation')
                diagnostics.append({'category': category, **detail})
                failures.append(f'{category}: model output failed validation')
                previous_coverage = coverage.get(category)
                coverage[category] = Coverage(category=category,
                    status='insufficient_information',
                    finding_ids=[i for i, f in findings.items() if f.category == category],
                    limitation='분석 호출·출력 검증 실패. 기존 결과가 있으면 유지. '
                        + ((previous_coverage.limitation or '') if previous_coverage else ''))
        for category in CATEGORIES:
            coverage.setdefault(category, Coverage(category=category, status='not_reviewed'))
        status = 'failed' if successful_categories == 0 else 'partial' if failures else 'complete'
        if (any(c.status == 'not_reviewed' for c in coverage.values())
                or (budget > 0 and calls >= budget and any(l.status == 'skipped' and l.query not in seen_queries
                                           for l in logs))) and status == 'complete':
            status = 'partial'
        analysis = RiskAnalysis(diagnostics=diagnostics, company_id=company_id, as_of=today, analysis_status=status,
            summary=' / '.join(f'{c}: {coverage[c].status}' for c in CATEGORIES),
            findings=list(findings.values()), coverage=[coverage[c] for c in CATEGORIES],
            sources=list(registry.values()), search_log=logs)
        response = None
        if req:
            old = {f.finding_id: f for f in prior.findings}
            changed = [i for i, f in findings.items() if i not in old or f != old[i]]
            new_sources = sorted(set(registry) - old_source_ids)
            answers = [question_answers.get(q.question_id) or QuestionResult(
                question_id=q.question_id, status='unanswered', answer='보완 결과를 확보하지 못함')
                for q in req.questions]
            unresolved = any(a.status != 'answered' for a in answers)
            outcome = ('search_failed' if any(l.status == 'failed' for l in logs[len(prior.search_log):])
                       else 'unresolved' if failures or ((changed or new_sources) and unresolved)
                       else 'updated' if changed or new_sources else 'no_new_evidence')
            response = ReviewResponse(request_id=req.request_id, outcome=outcome,
                updated_finding_ids=changed, new_source_ids=new_sources,
                change_summary=f'변경 항목 {len(changed)}개, 신규 수집 자료 {len(new_sources)}개. '
                    '신규 수집 자료 수는 독립 검증 근거 수를 의미하지 않음.',
                question_results=answers,
                remaining_unknowns=[u for f in findings.values() if f.category in categories for u in f.unknowns] + [u for c in categories for u in coverage[c].unknowns],
                search_limitations=failures + [coverage[c].limitation for c in categories if coverage[c].limitation])
        return RiskAgentOutput(risk_analysis=analysis, review_response=response)

    @staticmethod
    def _validate_result(result, category, sources, targets, req, as_of):
        if result.coverage.category != category or any(f.category != category for f in result.findings):
            raise ValueError('Out-of-scope category')
        ids = [f.finding_id for f in result.findings]
        if len(ids) != len(set(ids)) or set(result.coverage.finding_ids) != set(ids):
            raise ValueError('Invalid finding IDs')
        if req and req.finding_ids and set(ids) != {f.finding_id for f in targets}:
            raise ValueError('Review may modify only requested findings and must preserve their IDs')
        allowed = {s.source_id for s in sources.values() if not s.published_at or s.published_at <= as_of}
        used = set()
        for f in result.findings:
            used.update(f.mitigation.source_ids)
            for fact in f.facts:
                used.update(fact.source_ids)
        for q in result.question_results:
            if q.status in ('answered', 'partially_answered') and not q.source_ids:
                raise ValueError('Substantive review answers require evidence')
            used.update(q.source_ids)
        if not used <= allowed:
            raise ValueError('Fabricated or future source reference')
        question_ids = [q.question_id for q in result.question_results]
        expected = {q.question_id for q in req.questions} if req else set()
        if len(question_ids) != len(set(question_ids)) or not set(question_ids) <= expected:
            raise ValueError('Invalid question IDs')

    def __call__(self, state):
        req = (state.get('review_requests') or {}).get('risk')
        count = (state.get('retry_counts') or {}).get('risk', 0)
        if req and any(r.get('request_id') == req['request_id'] for r in state.get('risk_review_history', [])):
            raise ValueError('This review request was already processed')
        output = self.run(state['company_profile'], sources=state.get('company_evidence', []),
            previous=state.get('risk_analysis') if req else None, review_request=req,
            retry_count=count, as_of=state.get('as_of'))
        result = output.model_dump(mode='json')
        delta = {'risk_analysis': result['risk_analysis'],
                 'references': result['risk_analysis']['sources']}
        if output.review_response:
            delta['review_results'] = {'risk': result['review_response']}
            delta['retry_counts'] = {'risk': count + 1}
            delta['risk_review_history'] = state.get('risk_review_history', []) + [result['review_response']]
        return delta
