"""각 Agent 출력을 원래 형식 그대로 읽어 06이 쓰는 값(점수 입력·게이트·결측·근거·실사 질문)만 뽑는다.

State 키별 형식
- clinical_analysis: {company_id: Envelope}   → adapt_clinical(envelope)
- market_analysis  : Envelope                  → adapt_market(envelope)
- traction_analysis: Envelope                  → adapt_traction(envelope)
- risk_analysis    : risk-company-3 + references → adapt_risk(risk_analysis, references)
"""
from .clinical import adapt_clinical
from .market import adapt_market
from .risk import adapt_risk
from .stub import not_run
from .traction import adapt_traction

__all__ = ["adapt_clinical", "adapt_market", "adapt_traction", "adapt_risk", "not_run"]
