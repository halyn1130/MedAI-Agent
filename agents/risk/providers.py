"""Live adapters. Tests inject fakes; no credentials are needed to import."""
import json
import os
from datetime import date
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urlsplit

import requests

from .schema import CategoryResult, Source


class ProviderError(RuntimeError):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


class TavilySearch:
    def __init__(self, api_key=None):
        self.key = api_key or os.environ.get('TAVILY_API_KEY')
        if not self.key:
            raise ValueError('TAVILY_API_KEY is required')

    def search(self, query, *, company_id, as_of, request=None):
        payload = {'query': query, 'max_results': 4, 'include_raw_content': True,
                   'include_published_date': True}
        if request and request.published_after:
            payload['start_date'] = request.published_after.isoformat()
        payload['end_date'] = min(as_of, request.published_before or as_of).isoformat() if request else as_of.isoformat()
        try:
            response = requests.post('https://api.tavily.com/search',
                headers={'Authorization': f'Bearer {self.key}'}, json=payload, timeout=30)
            response.raise_for_status()
            records = response.json()['results']
            output = []
            for row in records:
                url = row.get('url', '')
                content = row.get('raw_content') or row.get('content') or ''
                if urlsplit(url).scheme not in ('http', 'https') or not content.strip():
                    continue
                sid = sha256(f'{company_id}|{url}|{content}'.encode()).hexdigest()[:20]
                published = None
                raw_date = row.get('published_date')
                if raw_date:
                    try:
                        published = date.fromisoformat(raw_date[:10])
                    except (TypeError, ValueError):
                        try:
                            published = parsedate_to_datetime(raw_date).date()
                        except (TypeError, ValueError, OverflowError):
                            pass
                output.append(Source(source_id=f'risk_{sid}', title=row.get('title') or url,
                    url=url, publisher=urlsplit(url).netloc, checked_at=date.today(),
                    published_at=published, content=content[:12000]))
            return output
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            # Never persist exception text containing request headers or credentials.
            raise ProviderError('Web search failed; check service access and configuration') from None


class ChatAnalyzer:
    def __init__(self, model=None):
        from langchain_openai import ChatOpenAI
        model = model or os.environ.get('RISK_MODEL')
        if not model or not os.environ.get('OPENAI_API_KEY'):
            raise ValueError('Set RISK_MODEL and OPENAI_API_KEY')
        self.chat = ChatOpenAI(model=model, temperature=0, timeout=60, max_retries=1)
        from openai.lib._pydantic import to_strict_json_schema
        schema = to_strict_json_schema(CategoryResult)
        self.structured = self.chat.with_structured_output(schema, method='json_schema', strict=True)

    def analyze(self, system_prompt, context):
        from .diagnostics import safe_diagnostic
        from pydantic import ValidationError
        messages = [
            ('system', system_prompt),
            ('human', json.dumps(context, ensure_ascii=False, default=str)),
        ]
        sources = {s['source_id']: s for s in context['sources']}
        for attempt in range(2):
            try:
                raw = self.structured.invoke(messages)
            except Exception as exc:
                raise ProviderError('Model call failed',
                    safe_diagnostic(exc, 'model_call_or_parse')) from None
            try:
                result = CategoryResult.model_validate(raw)
                for fi, finding in enumerate(result.findings):
                    for ti, fact in enumerate(finding.facts):
                        location = f'findings.{fi}.facts.{ti}'
                        if not fact.citations or set(fact.source_ids) != {c.source_id for c in fact.citations}:
                            raise EvidenceError('missing_source_quotation', location)
                        for citation in fact.citations:
                            source = sources.get(citation.source_id)
                            if not source:
                                raise EvidenceError('unknown_source_id', location)
                            if not citation.quote.strip():
                                raise EvidenceError('empty_quotation', location)
                            if citation.quote not in source['content']:
                                raise EvidenceError('quotation_not_in_source', location)
                return result
            except (ValidationError, EvidenceError) as exc:
                if isinstance(exc, EvidenceError):
                    detail = {'stage': 'evidence_validation', 'error_type': 'EvidenceError',
                              'code': exc.code, 'validation_fields': [exc.location],
                              'hint': 'Copy an exact quotation from the cited source; do not paraphrase quotations.'}
                else:
                    detail = safe_diagnostic(exc, 'response_schema')
                detail['attempts'] = attempt + 1
                if attempt == 1:
                    raise ProviderError('Output validation failed after one repair', detail) from None
                # Retry only this category. Raw output stays in memory, never in diagnostics.
                messages.append(('human', json.dumps({
                    'validation_feedback': detail,
                    'previous_output': raw,
                    'instruction': '같은 영역의 응답을 수정하세요. 인용문은 원문에서 그대로 복사하세요. 근거 없는 주장은 제거하고 coverage의 unknowns와 due_diligence_questions에 기록하세요. findings가 비어도 정상입니다. 지정 스키마의 전체 응답을 한국어로 반환하세요.',
                }, ensure_ascii=False, default=str)))


class EvidenceError(ValueError):
    def __init__(self, code, location):
        self.code, self.location = code, location
        super().__init__(code)
