"""Source contract used only by the standalone collector."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    title: str
    url: str
    publisher: str
    source_type: Literal['company_announcement', 'partner_announcement', 'official_record', 'news', 'other'] = 'other'
    published_at: date | None = None
    checked_at: date
    content: str = Field(min_length=1)

