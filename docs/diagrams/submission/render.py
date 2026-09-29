from pathlib import Path
import subprocess,json
O=Path(__file__).resolve().parents[3]/'output/images/산출물_플로우차트'
O.mkdir(parents=True,exist_ok=True)
from html import escape
class G:
 def __init__(self,file,title,sub):
  self.file=file
  self.s=['digraph G {',
   'graph [rankdir=TB, bgcolor="white", pad="0.32", nodesep="0.42", ranksep="0.50", splines=ortho, dpi=210, outputorder=edgesfirst];',
   'node [shape=box, style="rounded,filled", fillcolor="#F0F5FC", color="#93ACCC", penwidth=1.15, fontname="Apple SD Gothic Neo", fontsize=14, fontcolor="#253750", margin="0.22,0.18", width=3.2, height=0.72];',
   'edge [color="#8392A5", penwidth=1.25, arrowsize=0.7, fontname="Apple SD Gothic Neo", fontsize=11, fontcolor="#596A80"];']
 def n(self,k,t,kind='p'):
  attrs={'p':'','d':', shape=diamond, style=filled, fillcolor="#FFF5DD", color="#CFB777", width=3.0, height=1.05',
   'io':', shape=parallelogram, style=filled, fillcolor="#F4F6F9", color="#A3AFBF", width=3.6',
   'end':', shape=box, style="rounded,filled", fillcolor="#E7F3ED", color="#8CB7A0"',
   'bad':', fillcolor="#FBEDEA", color="#CFA59B"',
   'note':', shape=note, fillcolor="#F7F7F7", color="#B3BDC9"'}
  lines=t.split('\n')
  if kind=='d':
   label='<BR/>'.join('<B>'+escape(l)+'</B>' for l in lines)
  else:
   label='<B>'+escape(lines[0])+'</B>'
   for l in lines[1:]:label+='<BR/><FONT POINT-SIZE="12" COLOR="#627187">'+escape(l)+'</FONT>'
  self.s.append(k+' [label=<'+label+'>'+attrs[kind]+'];');return k
 def e(self,a,b,l='',dash=False):
  self.s.append(f'{a} -> {b} [xlabel='+json.dumps(l,ensure_ascii=False)+(',style=dashed,color="#9B90B6",fontcolor="#82719D"' if dash else '')+'];')
 def chain(self,*ks):
  for a,b in zip(ks,ks[1:]):self.e(a,b)
 def same(self,*ks):self.s.append('{rank=same; '+';'.join(ks)+';}')
 def save(self):
  self.s.append('}');p=O/(self.file+'.dot');p.write_text('\n'.join(self.s));subprocess.run(['dot','-Tpng',str(p),'-o',str(O/(self.file+'.png'))],check=True)
def reviewloop(g,out,back):
 g.n('send','결과를 06 투자 심사에 전달','end');g.e(out,'send')
 g.n('req','심사 보완 요청 수신\n기업당 최대 1라운드','io');g.n('patch','지정 항목만 추가 수집·수정\n기존 근거 ID·요청 외 결과 유지');g.e('send','req','요청이 있을 때',True);g.e('req','patch',dash=True);g.e('patch',back,'재검증 후 전체 결과 반환',True)
def common(g):g.n('ev','Source → Evidence → Finding\n미확인=null · 중복 출처 구분','note')
# overall
x=G('00_전체_플로우차트','Healthcare AI · 전체 플로우차트','설계안 | 고정 데이터셋 · 담당별 직접 수집 · 평가 정책 proposed-2.0')
for k,t,kind in [('csv','제공 후보 데이터셋','io'),('prep','01 데이터 입력·정규화\n추가 수집 없음','p'),('workers','기업별 독립 State 구성\n기업별 병렬 실행','p'),('c','02 임상·인허가\n자료 수집·분석','p'),('m','03 시장·사업성\n자료 수집·RAG·분석','p'),('t','04 실적·성장성\n자료 수집·분석','p'),('r','05 Risk\n운영 자료 수집·분석','p'),('join','06 결과 취합·근거 검증\n제품·국가·기간·출처 대조','p'),('need','보완 필요하며\n1회 한도 내인가?','d'),('supp','해당 Agent만 지정 항목 보완\n미해결 사항 보존','p'),('judge','06 판정·점수 계산\nproposed 정책 적용\n적격 / 부적격 / 판단불가','p'),('all','전체 후보 결과 취합\n실패·결측 기업도 포함','p'),('top','적격 기업 중 최대 K개 선정\n적격 0개도 결과 보고','p'),('report','투자 검토 보고서\n선정 이유 · 미선정 사유 · 근거·한계','end')]:x.n(k,t,kind)
x.chain('csv','prep','workers');x.same('c','m','t','r')
for k in ['c','m','t','r']:x.e('workers',k);x.e(k,'join')
x.chain('join','need');x.e('need','supp','예');x.e('supp','join','보완 결과',True);x.e('need','judge','아니요 / 한도 종료');x.chain('judge','all','top','report');x.save()
# input
x=G('01_데이터_입력_정규화','01 · 데이터 입력·정규화','제공 데이터셋만 사용 | 웹검색·크롤링·후보 추가 없음')
for k,t,kind in [('input','고정 CSV 데이터셋','io'),('read','파일 로딩·컬럼 매핑','p'),('norm','기업명·식별자 정규화\n중복 후보 확인','p'),('valid','기업을 식별할 수\n있는가?','d'),('missing','입력 오류·미확인 기록\n임의 정보 생성 금지','bad'),('scope','기업·대표 제품·목표국·기준일 구성\n제공된 값만 사용','p'),('opt','선택 입력 누락 허용\nmodality · hospital_type','p'),('out','company_profile · products · as_of\n기업별 공통 입력','io'),('end','02·03·04·05 분석에 전달','end'),('hold','판정에 필요한 식별 오류 전달\n06에서 판단불가 사유 기록','end')]:x.n(k,t,kind)
x.chain('input','read','norm','valid');x.e('valid','scope','예');x.e('valid','missing','아니요');x.chain('missing','hold');x.chain('scope','opt','out','end');x.save()
# clinical
x=G('02_서준영_임상_인허가','02 · 임상·인허가 분석 | 서준영','Healthcare AI | 공식 상태와 임상 근거를 구분')
for k,t,kind in [('input','기업·제품·용도·목표국 입력','io'),('scope','제품 식별 및 규제 적용 범위 조사','p'),('dec','규제 적용\n여부 확인','d'),('official','공식 기록 직접 수집\n허가·지정·등재·현재 조치','p'),('na','비적용 근거 기록\n비영상만으로 제외하지 않음','p'),('unk','범위 미확인 기록\n필수 누락·보완 질문 생성','bad'),('study','논문·시험·회사 주장 직접 수집\n규제 비대상도 효능 주장 검토','p'),('compare','제품·사용환경·연구 결과 대조\n상충 근거와 한계 기록','p'),('validate','출처·사실·판단 연결 검증\nC1 적용성·근거 수준·CL 플래그','p'),('out','clinical_analysis 반환\n공식 상태 · 점수/unknown · 보완 항목','io')]:x.n(k,t,kind)
x.chain('input','scope','dec');x.same('official','na','unk')
for k,l in [('official','적용'),('na','비적용'),('unk','미확인')]:x.e('dec',k,l);x.e(k,'study')
x.chain('study','compare','validate','out');reviewloop(x,'out','validate');x.save()
# market
x=G('03_안동선_시장_사업성','03 · 시장·사업성 분석 | 안동선','Healthcare AI 전체 범위 | 대표 제품 1개 · proposed 평가 정책')
for k,t,kind in [('input','제품유형 · 목표 고객 · 구매자\nmodality·병원유형은 선택','io'),('scope','시장 범위·사업유형별 체크 기준 설정\n지역·기간·통화·단위 고정','p'),('search','영역별 직접 수집·RAG 검색\n원문·페이지·발행일 보존','p'),('growth','시장 성장\nmarket_growth\nCAGR 기반','p'),('demand','고객·시장 수요\ndemand\nDM1~DM5','p'),('adopt','도입·상용화 가능성\nadoption\n사업유형별 AD 체크','p'),('money','수익화\nmonetization\nMO1~MO5','p'),('merge','근거 취합·타 분석과 사실 대조\n공식 상태는 임상 · 실제 계약은 실적','p'),('validate','제안 정책으로 C2·C3·C4 계산\n가중치·체크 기준은 팀 확정 전 proposed','p'),('out','market_analysis 반환\n절대 규모 별도 · unknown 유지\n시장 종합점수 중복 합산 금지','io')]:x.n(k,t,kind)
x.chain('input','scope','search');x.same('growth','demand','adopt','money')
for k in ['growth','demand','adopt','money']:x.e('search',k);x.e(k,'merge')
x.chain('merge','validate','out');reviewloop(x,'out','validate');x.save()
# traction
x=G('04_변현준_실적_성장성','04 · 실적·성장성 분석 | 변현준','직접 자료 수집 | 관측값 구조화 · 코드 계산')
for k,t,kind in [('input','기업·대표 제품·투자 단계 입력','io'),('search','공시·거래 상대·실증·고용 자료 수집','p'),('extract','매출·계약·고객·고용 관측값 추출\n기간·단위·제품 귀속 기록','p'),('verify','독립 근거·중복·철회 여부 확인\n허가·투자 유치를 매출로 인정하지 않음','p'),('stage','상업화 단계 S\nA 매출 / B 유료 계약\nC 실증 / D MOU·수상','p'),('growth','성장 추이 T\n비교 가능한 3개 결산연도 CAGR\n비교 불가=unknown','p'),('validate','C5·RF1~RF4 코드 계산\n성장 미확인 대체 규칙 명시','p'),('out','traction_analysis 반환\n관측값·근거·점수·한계','io')]:x.n(k,t,kind)
x.chain('input','search','extract','verify');x.same('stage','growth');x.e('verify','stage');x.e('verify','growth');x.e('stage','validate');x.e('growth','validate');x.e('validate','out');reviewloop(x,'out','validate');x.save()
# risk
x=G('05_유하린_Risk','05 · 운영·사업 지속성 Risk | 유하린','직접 자료 수집 | 현재 구현은 영역별 순차 처리 · C6는 설계안')
for k,t,kind in [('input','기업 프로필·기준일\n기존 결과·보완 요청 입력','io'),('check','입력·요청·검색 예산 검증','p'),('scope','조사할 영역 선택\n외부 자원 / 핵심 인력 / 운영 사건','p'),('search','선택 영역 자료 직접 검색\n날짜·출처·중복 확인','p'),('facts','사실·예상 영향·대응·미확인 분리','p'),('more','남은 영역이\n있는가?','d'),('validate','근거·항목 ID 검증 및 결과 취합\n운영 대비 OP1~OP5·C6 설계 적용','p'),('out','risk_analysis 반환\nfindings · coverage · 실사 질문\n자료 없음 ≠ 위험 없음','io')]:x.n(k,t,kind)
x.chain('input','check','scope','search','facts','more');x.e('more','search','예');x.e('more','validate','아니요');x.e('validate','out');reviewloop(x,'out','validate');x.save()
# review
x=G('06_김민솔_투자_심사_보고서','06 · 투자 심사·보고서 작성 | 김민솔','평가 정책 proposed-2.0 | 점수 계산은 Python · 설명은 근거 기반')
for k,t,kind in [('input','네 분석 결과·근거·실행 상태','io'),('valid','대상·기간·스키마·근거 충돌 검증','p'),('need','보완 필요하며\n1회 한도 내인가?','d'),('req','해당 Agent에 질문·근거·예산 전달\n보완 결과 취합','p'),('gate','G01·G02\n현재 중대 차단 확인?','d'),('unknown','필수 결측·충돌·검증 오류\n또는 적용 점수 unknown?','d'),('score','C1~C6 가중 총점 계산\nproposed-2.0 정책 적용\n중복 합산·임의 결측 보정 금지','p'),('pass','총점 ≥ 60이며\n모든 적용 항목 ≥ 2?','d'),('yes','적격','end'),('no','부적격\n차단 또는 점수 미달 사유','bad'),('hold','판단불가\n미확인·충돌·실패 사유','bad'),('all','전체 기업 결과 취합\n적격 기업 중 최대 K개 선정','p'),('report','보고서 생성\n선정·미선정 이유 · 점수·근거\n미확인·보완 이력·실사 질문','end')]:x.n(k,t,kind)
x.chain('input','valid','need');x.e('need','req','예');x.e('req','valid','보완 후',True);x.e('need','gate','아니요 / 종료');x.e('gate','no','예');x.e('gate','unknown','아니요');x.e('unknown','hold','예');x.e('unknown','score','아니요');x.chain('score','pass');x.e('pass','yes','예');x.e('pass','no','아니요');x.same('yes','no','hold')
for k in ['yes','no','hold']:x.e(k,'all')
x.chain('all','report');x.save()
print('Created 7 PNG diagrams')
