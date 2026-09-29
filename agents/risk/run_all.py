"""Fresh collection plus cached single-call company analysis."""
import argparse
import json
from pathlib import Path
from datetime import datetime,date
from concurrent.futures import ThreadPoolExecutor,as_completed
from hashlib import sha256
from dotenv import load_dotenv
from .collect import collect_company,load_companies
from .providers import TavilySearch
from .company_analysis import analyze,save_result


def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,default=Path('data/startup_list.csv'));p.add_argument('--run-dir',type=Path);p.add_argument('--limit',type=int,default=10000)
    args=p.parse_args();load_dotenv()
    root=args.run_dir or Path('agents/risk/data')/datetime.now().strftime('full_%Y%m%d_%H%M%S')
    root.mkdir(parents=True,exist_ok=True)
    profiles=load_companies(args.input)[:args.limit]
    def work(profile):
        folder=root/sha256(profile['company_id'].encode()).hexdigest()[:16]
        if not folder.exists():collect_company(profile,TavilySearch(),folder,date.today(),9)
        state_path=folder/'reviewed/state.json'
        if not state_path.exists():raise RuntimeError('Incomplete collection: inspect before retrying')
        state=json.loads(state_path.read_text())
        result=analyze(state,'agents/risk/data/model_cache')
        save_result(result,folder/'analysis.json')
        manifest=json.loads((folder/'manifest.json').read_text())
        return {'company':profile['company_name'],'folder':folder.name,'status':result['analysis_status'],
                'mode':result['mode'],'observations':sum(len(a['observations']) for a in result['areas']),
                'diagnostics':len(result['diagnostics']),'sources':manifest['source_count'],'search_failures':manifest['failed_calls']}
    rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks={pool.submit(work,p):p for p in profiles}
        for f in as_completed(tasks):
            try:r=f.result()
            except Exception as e:r={'company':tasks[f]['company_name'],'status':'failed','error_type':type(e).__name__}
            rows.append(r);(root/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
            print(f"{len(rows)}/{len(profiles)} {r['company']}: {r['status']}",flush=True)
    lines=['# 수집·분석 결과','','complete는 실행 상태이며 사실성·투자 적격을 보장하지 않습니다.','', '| 기업 | 상태 | 처리 | 관측 사실 | 검증 보류 | 결과 |','|---|---|---|---:|---:|---|']
    for r in sorted(rows,key=lambda r:r['company']):
        link=f"[분석]({r['folder']}/analysis.md)" if 'folder' in r else '실패'
        lines.append(f"| {r['company']} | {r['status']} | {r.get('mode','error')} | {r.get('observations',0)} | {r.get('diagnostics',0)} | {link} |")
    (root/'SUMMARY.md').write_text('\n'.join(lines)+'\n');print('OUTPUT',root,flush=True)
if __name__=='__main__':main()
