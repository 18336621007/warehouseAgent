# ── state/planner.py ──
# Planner 独有字段 + 跨模块只读字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState

class PlannerState(BaseState, total=False):
    route: str                   # "seeker" / "advisor"
    planner_reason: str          # 路由原因
    planner_entities: dict       # {tables, fields, completeness}

    # Planner 置信度用于控制澄清和直接查询的边界
    planner_confidence: float

    confirmed_plan: dict         # Advisor 写入，Planner 只读