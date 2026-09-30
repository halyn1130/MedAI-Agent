"""Model adapter and compatibility exports for existing integrations."""
import json
import os

from .schema import CategoryResult
from .errors import ProviderError
from .collection.search import TavilySearch


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
