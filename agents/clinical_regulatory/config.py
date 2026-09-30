from __future__ import annotations

import os
import threading
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════
# 설정 (권장 기본안 · 팀 합의 후 조정)
# ══════════════════════════════════════════════
AGENT = "clinical"
MAX_REVIEW_ROUNDS = int(os.getenv("MAX_REVIEW_ROUNDS", "1"))
COMPANY_CONCURRENCY = int(os.getenv("COMPANY_CONCURRENCY", "5"))
CALL_CONCURRENCY = int(os.getenv("CALL_CONCURRENCY", "8"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
WEB_RETRY = int(os.getenv("WEB_RETRY", "4"))  # 웹 검색 재시도 (3→6→12→24초 대기)
TRANSIENT_RETRY = 1                     # 일시 오류 재시도 (호출 예산에 포함)
SECTION_CHAR_LIMIT = 12000              # LLM 입력 섹션별 최대 글자 수
DEFAULT_TARGET_COUNTRIES = ["KR"]

C1_LEVEL_SCORE = {"L1": 1, "L2": 2, "L3": 4, "L4": 5}
C1_NOT_APPLICABLE_TYPES = set(os.getenv("C1_NOT_APPLICABLE_TYPES", "service").split(","))
LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

MFDS_API_KEY = urllib.parse.unquote(os.getenv("MFDS_API_KEY", ""))  # 포털 키는 URL 인코딩 상태로 발급됨
MFDS_PERMIT = {
    "name": "식약처 의료기기 품목허가",
    "base": "https://apis.data.go.kr/1471000/MdlpPrdlstPrmisnInfoService06",
    "operation": os.getenv("MFDS_PERMIT_OPERATION", ""),
    "company_param": os.getenv("MFDS_PERMIT_COMPANY_PARAM", ""),
    "product_param": os.getenv("MFDS_PERMIT_PRODUCT_PARAM", ""),
}
MFDS_ITEM = {
    "name": "식약처 의료기기 품목정보",
    "base": "https://apis.data.go.kr/1471000/MdeqPrdlstInfoService02",
    "operation": os.getenv("MFDS_ITEM_OPERATION", ""),
    "company_param": os.getenv("MFDS_ITEM_COMPANY_PARAM", ""),
    "product_param": os.getenv("MFDS_ITEM_PRODUCT_PARAM", ""),
}

MFDS_ITEM_MAX_PER_COMPANY = int(os.getenv("MFDS_ITEM_MAX_PER_COMPANY", "5"))  # 품목정보 상세 조회 최대 건수

COLLECT_ON = os.getenv("CLINICAL_COLLECT", "on").lower() in ("on", "1", "true")   # CSV만 입력될 때 자체 수집
COLLECT_MAX_PAGES = int(os.getenv("COLLECT_MAX_PAGES", "5"))

STEP_ORDER = ["classify", "regulatory", "institutional", "studies", "claims"]
AREA_BY_STEP = {
    "classify": ["classification"], "regulatory": ["regulatory"],
    "institutional": ["designation", "reimbursement"], "studies": ["clinical_studies"], "claims": ["claim_check"],
}
CATEGORY_TO_STEPS = {
    "applicability": STEP_ORDER,
    "regulatory": ["regulatory", "claims"],
    "designation": ["institutional"],
    "reimbursement": ["institutional"],
    "clinical_evidence": ["studies", "claims"],
    "claim": ["claims"],
    "red_flag:CL01": ["regulatory", "studies", "claims"],
    "red_flag:CL02": ["regulatory"],
    "red_flag:CL03": ["studies", "claims"],
}
KEYWORD_TO_STEPS = [
    (("유형", "규제 대상", "적용 범위", "분류", "비대상"), STEP_ORDER),
    (("허가", "인증", "신고", "식약처", "FDA", "510", "CE", "사용 목적", "사용목적"), ["regulatory", "claims"]),
    (("지정", "혁신의료기기", "급여", "등재", "혁신의료기술", "신의료기술", "수가", "보험"), ["institutional"]),
    (("임상", "연구", "논문", "근거", "검증", "성능", "민감도", "특이도"), ["studies", "claims"]),
    (("주장", "홍보", "광고"), ["claims"]),
]

_CALL_SEM = threading.Semaphore(CALL_CONCURRENCY)

# 파일 캐시: CLINICAL_CACHE=on 이면 외부 조회 응답을 저장하고, 같은 조회는 파일에서 읽는다 (호출 예산·트래픽 미사용)
CACHE_ON = os.getenv("CLINICAL_CACHE", "off").lower() in ("on", "1", "true")
CACHE_DIR = Path(os.getenv("CLINICAL_CACHE_DIR", ".cache/clinical"))
RESULT_DIR = os.getenv("CLINICAL_RESULT_DIR", "clinical_results")  # 지정하면 기업별 결과를 즉시 저장 (중단 대비)
