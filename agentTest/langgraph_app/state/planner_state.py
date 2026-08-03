# ── state/planner.py ──
# Planner 独有字段 + 跨模块只读字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState
from agentTest.langgraph_app.state.planner_handoff_state import PlannerHandoffState
from agentTest.langgraph_app.state.query_plan import QueryPlan

class PlannerState(BaseState, PlannerHandoffState, total=False):
    route: str                   # "seeker" / "advisor"

    # Planner 置信度用于控制澄清和直接查询的边界
    planner_confidence: float

    # Planner 负责检查方案并处理最终确认
    confirmed_plan: QueryPlan
