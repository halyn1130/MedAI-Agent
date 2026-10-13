"""python -m agents.risk --input worker_input.json --output outputs/risk.json"""
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from .evaluator import evaluate_risk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    load_dotenv()
    result = evaluate_risk(json.loads(args.input.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Risk evaluation: {result['analysis_status']} -> {args.output}")
    if result['analysis_status'] == 'failed': raise SystemExit(1)


if __name__ == '__main__':
    main()
