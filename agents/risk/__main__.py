"""python -m agents.risk --input state.json --output output.json [--demo]"""
import argparse
import json
from pathlib import Path

from .agent import RiskAgent
from .providers import ChatAnalyzer, TavilySearch
from .schema import CategoryResult, Coverage, QuestionResult

class DemoSearch:
    def search(self, query, **kwargs):
        return []


class DemoAnalyzer:
    """Offline wiring check only. Does NOT simulate factual company analysis."""
    def analyze(self, prompt, context):
        req = context['review_request']
        return CategoryResult(
            findings=context['previous_findings'],
            coverage=Coverage(category=context['category'], status='insufficient_information',
                finding_ids=[f['finding_id'] for f in context['previous_findings']],
                limitation='DEMO: 실제 검색·LLM 분석을 수행하지 않음'),
            question_results=[QuestionResult(question_id=q['question_id'], status='unanswered',
                answer='DEMO: 실제 검증하지 않음') for q in req['questions']] if req else [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--max-search-calls', type=int, default=6)
    parser.add_argument('--max-reviews', type=int, default=1)
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    state = json.loads(args.input.read_text(encoding='utf-8-sig'))
    if not args.demo:
        from .company_analysis import analyze, save_result
        result = analyze(state, Path(__file__).parent / 'data/model_cache')
        save_result(result, args.output)
        print(f"Risk analysis: {result['analysis_status']} -> {args.output}")
        if result['analysis_status'] == 'failed': raise SystemExit(1)
        return
    search = DemoSearch() if args.demo or args.max_search_calls == 0 else TavilySearch()
    analyzer = DemoAnalyzer() if args.demo else ChatAnalyzer()
    agent = RiskAgent(search, analyzer, max_search_calls=args.max_search_calls, max_reviews=args.max_reviews)
    result = agent(state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    status = result['risk_analysis']['analysis_status']
    print(f'Risk analysis: {status} -> {args.output}')
    if status == 'failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
