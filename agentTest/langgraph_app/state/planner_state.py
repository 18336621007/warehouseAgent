# ── state/planner.py ──
# Planner 独有字段 + 跨模块只读字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState
from agentTest.langgraph_app.state.query_plan import QueryPlan

class PlannerState(BaseState, total=False):
    route: str                   # "seeker" / "advisor"
    planner_reason: str          # 路由原因
    planner_entities: dict       # {effective_query, tables, fields, completeness}

    # Planner 置信度用于控制澄清和直接查询的边界
    planner_confidence: float

    # Planner 负责检查方案并处理最终确认
    confirmed_plan: QueryPlan
