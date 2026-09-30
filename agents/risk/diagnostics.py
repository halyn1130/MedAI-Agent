"""Safe error metadata: never serialize exception messages, inputs, or responses."""
from pydantic import ValidationError


def safe_diagnostic(exc, stage):
    result = {'stage': stage, 'error_type': type(exc).__name__}
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int):
        result['http_status'] = status
    hints = {401: 'Check API credentials.', 403: 'Check model/project permissions.',
             429: 'Check billing quota and rate limits.', 400: 'Check model parameters and schema.',
             404: 'Check model ID and endpoint.'}
    result['hint'] = hints.get(status, 'Check connection, model response and output schema.')
    if isinstance(exc, ValidationError):
        # Only schema field names and numeric array positions are retained.
        from . import schema
        from pydantic import BaseModel
        allowed = set()
        for value in vars(schema).values():
            if isinstance(value, type) and issubclass(value, BaseModel):
                allowed.update(value.model_fields)
        result['validation_fields'] = [
            '.'.join(str(x) if isinstance(x, int) or x in allowed else '<field>' for x in item['loc'])
            for item in exc.errors(include_input=False, include_context=False, include_url=False)[:20]
        ]
        result['hint'] = 'Output does not match schema; inspect validation_fields.'
    return result
