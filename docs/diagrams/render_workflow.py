"""Build the presentation SVG; render PNG with macOS Quick Look."""
from pathlib import Path
from html import escape

ROOT = Path(__file__).resolve().parent
parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1680" height="1390" viewBox="0 0 1680 1390">', '''<defs>
<marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="#64748b"/></marker>
<marker id="amber" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="#b7791f"/></marker>
<marker id="green" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="#16806a"/></marker>
</defs><rect width="1680" height="1390" fill="#f7f9fc"/>
<g font-family="Apple SD Gothic Neo, sans-serif">''']

def text(x,y,t,size=18,color='#475569',weight=400):
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(t)}</text>')

def rect(x,y,w,h,fill='#ffffff',stroke='#dfe6ee',r=16):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}"/>')

def line(d,color='#64748b',marker='arrow',dash=False):
    parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" marker-end="url(#{marker})"'+(' stroke-dasharray="6 5"' if dash else '')+'/>')

def card(x,y,w,h,title,lines,fill='#fff',stroke='#dfe6ee',titlecolor='#15273f'):
    rect(x,y,w,h,fill,stroke)
    text(x+22,y+35,title,22,titlecolor,700)
    for i,t in enumerate(lines): text(x+22,y+68+i*27,t,18)

text(60,56,'MedAI-Agent / MULTI-AGENT WORKFLOW',18,'#4263aa',700)
text(60,103,'7개 에이전트가 협업하는 투자 평가 흐름',36,'#15273f',700)
text(60,142,'기업·제품 조사 → 4개 분야 병렬 분석 → 투자 심사 → 보고서 작성',21)
rect(1440,57,180,38,'#e9eef6','#e9eef6',19)
text(1465,83,'DESIGN / 설계안',17,'#52647e',600)
card(60,183,465,120,'후보 데이터', ['10~30개 · 비상장 · Series B~C', 'Exit 미완료 · AI 핵심 제품'])
card(545,183,575,120,'시장·사업성 RAG 지식베이스', ['시장·산업 보고서 → 청크 → 임베딩 → 인덱스', '200페이지 이하 · 모델 실측 비교 · 출처 보존'])
card(1140,183,480,120,'평가 정책', ['6항목 가중치 · 임계값 · 레드플래그', '정보 없음 규칙 · 후보 수 / 보완 횟수 제한'])

# Exhaustion route is separate from the per-company analysis loop.
line('M 205 600 V 347 H 1490 V 600','#b7791f','amber')
rect(480,330,810,34,'#f7f9fc','#f7f9fc',0)
text(496,354,'후보 없음 / 전원 보류 / 후보 상한 도달 → 종료 보고서',19,'#9a651b',600)

# Four analysis agents share a profile but write independent results.
rect(460,437,560,508,'#eef2f8','#dce4ef',20)
text(490,474,'병렬 분석',23,'#15273f',700)
text(625,474,'동일 기업 프로필 · 독립된 분석 결과',17)
for y,title,lines,rag in [
    (495,'2  임상·인허가 분석',['임상 검증 · 허가 상태와 사용 목적', '기업의 성능 주장과 검증 근거 구분'],False),
    (605,'3  시장·사업성 분석',['시장 규모·수요 · 병원 도입 · 수익 모델', '시장·산업 보고서에서 근거 검색'],True),
    (715,'4  실적·성장성 분석',['매출·계약·유료 고객 · 도입 성과·기간별 변화', 'MOU·실증·상용 구분 / 투자 유치와 매출 분리'],False),
    (825,'5  리스크 분석',['고객 집중 · 계약 불확실성 · 데이터 의존성', '공개 근거·미확인 사항 → 추가 실사 질문'],False)]:
    card(490,y,500,100,title,lines,'#f5f0ff' if rag else '#ffffff','#ded4ee' if rag else '#dfe6ee')
    if rag:
        rect(903,y+13,67,28,'#e5daf6','#e5daf6',14)
        text(918,y+34,'RAG',15,'#7757a6',700)

card(60,600,320,240,'1  기업·제품 조사', ['기업 정보·후보 적격성 확인', '제품·AI 기능·대상 고객 정리', '공개 기술 설명·논문 링크', '기술 우수성은 판정하지 않음', '주장별 출처·기준일·확인 상태', '공통 기업 프로필 생성'], '#eef4ff','#cfddf5')
text(70,872,'적격 후보 프로필을 4개 Agent에 전달',17,'#64748b')
# Branch bus and join bus; no agents read each other’s unfinished outputs.
line('M 380 690 H 420')
parts.append('<path d="M420 545 V875" fill="none" stroke="#64748b" stroke-width="2"/>')
for y in (545,655,765,875): line(f'M420 {y} H490')
for y in (545,655,765,875):
    parts.append(f'<path d="M990 {y} H1040" fill="none" stroke="#64748b" stroke-width="2"/>')
parts.append('<path d="M1040 545 V875" fill="none" stroke="#64748b" stroke-width="2"/>')
line('M1040 690 H1100')
text(1039,920,'모두 완료 후 취합',16,'#64748b')
card(1100,600,245,210,'6  투자 심사', ['근거 충돌·누락 확인', '6항목 평가·레드플래그', 'Python 함수로 점수 계산', '정책 적용 → 투자 / 보류', '필요 시 담당 분석에 보완'], '#eef4ff','#cfddf5')
card(1390,600,230,180,'7  보고서 작성', ['점수·판단 근거', '기업·시장·팀·리스크', '분석 한계·REFERENCE', '투자 / 종료 보고서'], '#eaf7f2','#b8dfd1')
line('M1345 690 H1390','#16806a','green')
text(1350,670,'투자',15,'#16806a',700)

# Targeted revision loop enters the group header rather than forcing all reruns.
line('M1220 600 V400 H740 V437','#64748b','arrow',True)
rect(780,381,420,32,'#f7f9fc','#f7f9fc',0)
text(790,404,'근거 부족·충돌 → 해당 Agent만 보완',18,'#52647e',600)
# Hold loop is routed below the parallel group.
line('M1220 810 V997 H205 V840','#b7791f','amber')
rect(430,979,700,34,'#f7f9fc','#f7f9fc',0)
text(442,1003,'보류 → 결과 보존 · 다음 후보 이동 · 기업별 State 초기화',18,'#9a651b',600)

text(60,1070,'공통 실행 규칙',21,'#15273f',700)
card(60,1092,510,205,'RAG · 보완 요청', [
'Hybrid 검색: BM25 + Dense · Reranker 선택',
'시장·사업성 Agent에만 RAG 적용',
'시장: RAG 재검색 / 나머지: 자료 조회·재분석',
'상한 도달 시 정보 없음 규칙으로 투자/보류 결정'], '#f5f0ff','#ded4ee')
card(590,1092,500,205,'공유 State · 외부 도구', [
'동일 기업 프로필 → 4개 분야 독립 분석',
'evidence / references는 Reducer로 누적',
'후보 변경 시 결과 보존 · 분석·재시도 초기화',
'웹검색 · DART 교차 확인 · KIPRIS(선택)'])
card(1110,1092,510,205,'보고서 출력 · 종료', [
'Markdown → PDF · 수치와 인용 검증',
'전체 5페이지 · SUMMARY 1/2페이지 이내',
'analysis.json / report.md / report.pdf',
'첫 투자 추천 또는 후보 소진 시 보고서 후 종료'], '#eaf7f2','#b8dfd1')
text(60,1349,'실선: 실행 흐름   |   점선: 해당 분석 보완   |   주황: 다음 후보 / 종료 분기',17)
text(1190,1349,'모델·가중치·임계값은 팀 검토 후 확정',15)
parts.append('</g></svg>')
(ROOT/'workflow.svg').write_text('\n'.join(parts))
