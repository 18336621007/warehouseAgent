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

    # Planner 是唯一路由者，负责判定 seeker/advisor 并落盘最终方案
    confirmed_plan: QueryPlan         # 当前查询方案（draft/locked/confirmed）

    # 连续问答类型：new_query / plan_refinement / result_follow_up / clarification_explanation
    follow_up_mode: str
