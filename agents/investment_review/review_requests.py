"""검증 문제 → Agent별 보완 요청을 각 Agent가 받는 형식 그대로 생성.
traction: state["review_requests"] list에 schema.ReviewRequest 형식(target_agent="traction").
risk: state["review_requests"]["risk"] = {company_id, attempt=1, reason, questions:[str]}."""
