"""Tavily public evidence search adapter."""
import os
from datetime import date
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urlsplit

import requests

from ..errors import ProviderError
from ..schema import Source


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


