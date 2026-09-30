from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from langchain.chat_models import init_chat_model

try:
    from .config import _CALL_SEM
    from .context import ClinicalRun
    from .utils import _clip
except ImportError:
    from config import _CALL_SEM
    from context import ClinicalRun
    from utils import _clip


# ══════════════════════════════════════════════
# 프롬프트 · LLM
# ══════════════════════════════════════════════
@lru_cache(maxsize=1)
def load_prompts() -> dict[str, str]:
    text = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
    parts = re.split(r"<!--\s*section:\s*([a-z_]+)\s*-->", text)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts), 2)}


@lru_cache(maxsize=1)
def _llm():
    return init_chat_model(os.getenv("LLM_MODEL", "gpt-4o-mini"),
                           model_provider=os.getenv("LLM_PROVIDER", "openai"), temperature=0)


def ask(schema, section: str, payload: dict):
    prompts = load_prompts()
    data = "\n".join(f"## {k}\n{_clip(v)}" for k, v in payload.items())
    messages = [("system", prompts["system"]), ("human", f"{prompts[section]}\n\n# 입력 데이터\n{data}")]
    with _CALL_SEM:
        return _llm().with_structured_output(schema).invoke(messages)


def _product_brief(ctx: ClinicalRun, pid: str) -> dict:
    p = ctx.product(pid)
    return {"product_id": pid, "name": p.name, "model": p.model, "version": p.version,
            "manufacturer": p.manufacturer, "intended_use": p.intended_use, "target_disease": p.target_disease,
            "users": p.users, "environment": p.environment, "modality": p.modality,
            "target_countries": p.target_countries, "claims": [c.model_dump() for c in p.claims]}


def _company_brief(ctx: ClinicalRun) -> dict:
    c = ctx.company
    return {"company_id": c.company_id, "display_name": c.display_name, "legal_name": c.legal_name,
            "aliases": c.aliases, "english_name": c.english_name, "description": c.description,
            "as_of": ctx.as_of}
