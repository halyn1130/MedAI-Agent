"""python -m agents.risk --company-id ID --output outputs/risk.json"""
import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from .agent import RiskAgent
from .dataset import DEFAULT_MANIFEST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--company-id', required=True)
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    load_dotenv()
    result = RiskAgent(manifest_path=args.manifest)(
        {'company_profile': {'company_id': args.company_id}})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    status = result['risk_analysis']['analysis_status']
    print(f'Risk analysis: {status} -> {args.output}')
    if status == 'failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
