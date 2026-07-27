# ── state/planner.py ──
# Planner 独有字段 + 跨模块只读字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState

class PlannerState(BaseState, total=False):
    route: str                   # "seeker" / "advisor"
    planner_reason: str          # 路由原因
    planner_entities: dict       # {tables, fields, completeness}
    confirmed_plan: dict         # Advisor 写入，Planner 只读
    advisor_last_answer: str     # Advisor 上轮回复，Planner 用于理解用户选择