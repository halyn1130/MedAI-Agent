"""Render the Agents diagram with explicit positions and orthogonal routes.

python3 docs/diagrams/presentation/render_medai_agents.py
rsvg-convert -o docs/diagrams/presentation/medai_agents.png docs/diagrams/presentation/medai_agents.svg
"""
from pathlib import Path
from html import escape

parts = ['''<svg xmlns="http://www.w3.org/2000/svg" width="1240" height="1130" viewBox="0 0 1240 1130">
<defs><marker id="gray" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#94a3b8"/></marker><marker id="blue" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#3b82f6"/></marker><marker id="gold" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#c99a40"/></marker></defs>
<rect width="1240" height="1130" fill="white"/>
<g font-family="Apple SD Gothic Neo, sans-serif" fill="#243247">''']
def text(x,y,value,size=14,color='#64748b',anchor='middle'):
    parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" fill="{color}">{escape(value)}</text>')
def rect(x,y,w,h,fill,stroke):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
def node(x,y,w,title,lines,kind='gray',h=84):
    colors={'gray':('#fff','#d7dfeb'),'blue':('#eff6ff','#93c5fd'),'purple':('#eef2ff','#a5b4fc'),'gold':('#fffbeb','#fcd34d'),'teal':('#f0fdfa','#5eead4')}
    rect(x,y,w,h,*colors[kind]); text(x+w/2,y+29,title,17,'#243247')
    for i,line in enumerate(lines): text(x+w/2,y+53+i*21,line,14,'#475569')
def edge(points,kind='gray',arrow=True):
    color={'gray':'#94a3b8','blue':'#3b82f6','gold':'#c99a40'}[kind]
    dash=' stroke-dasharray="7 6"' if kind=='blue' else ''
    marker=f' marker-end="url(#{kind})"' if arrow else ''
    parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="miter"{dash}{marker}/>')
text(620,45,'MedAI-Agent',27,'#172554')
text(620,74,'Agent 구성과 데이터 보완 흐름',17)
rect(75,325,1070,160,'#f8faff','#e0e7ff')
text(95,351,'전문 Worker · 선택된 분야 병렬 평가',14,'#6366a0','start')
rect(835,754,335,320,'#f8fbff','#bfdbfe')
text(852,779,'데이터 보완 · 최대 1회',14,'#2563eb','start')
# Main flow and parallel worker buses.
edge('570,198 570,230')
edge('570,314 570,365',arrow=False)
edge('210,365 990,365',arrow=False)
for x in (210,470,730,990):
    edge(f'{x},365 {x},380')
    edge(f'{x},464 {x},505',arrow=False)
edge('210,505 990,505',arrow=False)
edge('570,505 570,535')
edge('570,619 570,665')
edge('570,749 570,805')
edge('570,889 570,975')
# Review loop runs outside the Worker group.
edge('420,577 35,577 35,272 420,272','gold')
text(48,555,'선택적 보완 · 최대 1라운드',13,'#a16207','start')
# Collection branch and return lanes.
edge('720,847 855,847','blue')
text(786,833,'데이터 부족',13,'#2563eb')
edge('1002,899 1002,961','blue')
text(1015,932,'새 자료 있음',13,'#2563eb','start')
edge('1002,805 1002,707 720,707','blue')
text(860,694,'새 자료 없음 · 재평가 생략',13,'#2563eb')
edge('1150,1003 1205,1003 1205,644 690,644 690,665','blue')
text(1135,632,'재선정',13,'#2563eb')
node(420,114,300,'Data Collect',['취합본 로딩 · 신규 수집'],'blue')
node(420,230,300,'Orchestrator',['Worker 선택 · 입력 분배'],'purple')
for x,title,lines in [(95,'Clinical / Regulatory',['임상 · 인허가 / C1']), (355,'Market',['시장 · 수요 · 수익화 / C2·C3·C4']), (615,'Traction / Growth',['상업화 실적 · 성장 / C5']), (875,'Risk',['의존성 · 인력 · 운영 / C6'])]:
    node(x,380,230,title,lines)
node(420,535,300,'Quality Check',['근거 · 충돌 · 결측 검증'],'gold')
node(420,665,300,'Finalize / Select',['채점 · 판정 · 기업 선정'],'teal')
node(420,805,300,'Aggregate Results',['결과 취합 · 데이터 부족 판단'],'gold')
text(558,934,'자료 충분 / 보완 한도 도달',13,anchor='end')
node(420,975,300,'Report',['확인 사실 중심 요약 · 선정 이유','작성 → 품질 평가 · 재작성/재검토'],'teal',94)
node(855,805,295,'Recollect Plan / Company',['요청 생성 → 기업별 병렬 재수집','recollect.py · 데이터셋 저장'],'blue',94)
node(855,961,295,'관련 Worker 재평가 · 재판정',['기존 선택 Worker 중','새 자료가 붙은 Worker만'],'purple',94)
parts.append('</g></svg>')
Path(__file__).with_name('medai_agents.svg').write_text('\n'.join(parts))
