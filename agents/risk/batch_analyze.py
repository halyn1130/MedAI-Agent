"""Analyze reviewed collection folders without new web searches."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from .agent import RiskAgent
from .providers import ChatAnalyzer
from .schema import CategoryResult, Coverage
from .quality import QUESTIONS, CATEGORIES


class NoEvidence:
    def analyze(self, prompt, context):
        group = next(g for g,c in CATEGORIES.items() if c == context['category'])
        return CategoryResult(coverage=Coverage(category=context['category'], status='insufficient_information',
            limitation='선별 자료 0건. GPT 호출 없이 미확인 처리.', unknowns=['판단에 사용할 공개 근거 미확보'],
            due_diligence_questions=QUESTIONS[group]))


def process(path, out):
    from .company_analysis import analyze, save_result
    state=json.loads(path.read_text())
    result=analyze(state, Path(__file__).parent/'data/model_cache')
    folder=out/path.parent.parent.name
    save_result(result,folder/'analysis.json')
    return {'company':result['company_name'],'company_id':result['company_id'],
            'status':result['analysis_status'],'mode':result['mode'],
            'findings':sum(len(a['observations']) for a in result['areas']),
            'diagnostics':result['diagnostics'],'folder':folder.name}


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-dir',required=True,type=Path);args=p.parse_args()
    load_dotenv()
    out=args.input_dir/('analysis_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir()
    paths=sorted(args.input_dir.glob('*/reviewed/state.json'))
    rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(process,path,out):path for path in paths}
        for future in as_completed(futures):
            try: row=future.result()
            except Exception as exc:
                row={'company':futures[future].parent.parent.name,'status':'failed','mode':'execution_error','findings':0,'folder':futures[future].parent.parent.name,'error_type':type(exc).__name__}
            rows.append(row)
            (out/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
            print(f"{len(rows)}/{len(paths)} {row['company']}: {row['status']} ({row['mode']})",flush=True)
    lines=['# 전체 Risk 분석 결과','','추가 웹검색 없음. 자료 0건은 GPT 호출 없이 미확인 처리. complete는 실행 상태입니다.','', '| 기업 | 처리 | 실행 상태 | 판단 수 | 결과 |','|---|---|---|---:|---|']
    for r in sorted(rows,key=lambda r:r['company']):
        link=f"[분석]({r['folder']}/analysis.md)" if (out/r['folder']/'analysis.md').exists() else '실행 실패'
        lines.append(f"| {r['company']} | {r['mode']} | {r['status']} | {r['findings']} | {link} |")
    (out/'SUMMARY.md').write_text('\n'.join(lines)+'\n')
    print('OUTPUT:',out,flush=True)

if __name__=='__main__': main()
