"""Offline replay and citation audit; never permits a model request."""
import argparse,json
from pathlib import Path
from collections import Counter
from .company_analysis import analyze,save_result


def main():
    p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True,type=Path);args=p.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    root=args.run_dir;rows=[];issues=[];counts=Counter();usage=Counter()
    def forbidden(_):raise RuntimeError('Offline audit attempted API call')
    for file in sorted(root.glob('*/reviewed/state.json')):
        state=json.loads(file.read_text())
        # Check cache identity by using the saved result key before any replay.
        old=json.loads((file.parent.parent/'analysis.json').read_text())
        if old['mode']!='no_evidence':
            assert (Path('agents/risk/data/model_cache')/(old['cache_key']+'.json')).exists()
        result=analyze(state,'agents/risk/data/model_cache',caller=forbidden)
        assert result['mode'] in ('cache','no_evidence','blocked'), 'Unexpected cache miss'
        sources={s['source_id']:s for s in state['company_evidence']}
        for area in result['areas']:
            for o in area['observations']:
                for c in o['citations']:
                    assert sources[c['source_id']]['content'][c['start']:c['end']]==c['text']
                    assert c['url']==sources[c['source_id']]['url']
                counts['observations']+=1
                if o['kind']=='risk_signal':counts['risk_signals']+=1
        counts[result['analysis_status']]+=1
        counts['held_items']+=len(result['diagnostics'])
        for k,v in (result.get('usage') or {}).items():
            if isinstance(v,int):usage[k]+=v
        save_result(result,file.parent.parent/'analysis_verified.json')
        rows.append({'company':result['company_name'],'status':result['analysis_status'],'observations':sum(len(a['observations']) for a in result['areas']),'held':len(result['diagnostics']),'folder':file.parent.parent.name})
    lines=['# 재수집·GPT 분석 및 오프라인 검증','','검증 과정의 GPT/Tavily 호출: 0회. 모든 인용의 원문 위치·본문·URL 일치 확인.','', '실행 상태는 사실성 보장이 아닙니다. 의미 검토가 필요한 자료입니다.','',json.dumps(dict(counts),ensure_ascii=False),'','| 기업 | 실행 상태 | 관측 | 보류 | 결과 |','|---|---|---:|---:|---|']
    for r in sorted(rows,key=lambda r:r['company']):lines.append(f"| {r['company']} | {r['status']} | {r['observations']} | {r['held']} | [검증 결과]({r['folder']}/analysis_verified.md) |")
    (root/'VERIFICATION.md').write_text('\n'.join(lines)+'\n')
    (root/'verification.json').write_text(json.dumps({'companies':len(rows),'counts':dict(counts),'usage':dict(usage),'rows':rows},ensure_ascii=False,indent=2))
    print(json.dumps({'companies':len(rows),'counts':dict(counts),'usage':dict(usage)},ensure_ascii=False))
if __name__=='__main__':main()
