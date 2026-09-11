# ── state/planner.py ──
# Planner 独有字段 + 跨模块只读字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState
from agentTest.langgraph_app.state.planner_handoff_state import PlannerHandoffState
from agentTest.langgraph_app.state.query_plan import QueryPlan

class PlannerState(BaseState, PlannerHandoffState, total=False):
    route: str                   # "execute" / "respond"（A1 收敛两值）

    # Planner 置信度用于控制澄清和直接查询的边界
    planner_confidence: float

    # Planner 是唯一决策者：判定 execute（进执行链）/ respond（给用户文本）并落盘最终方案
    confirmed_plan: QueryPlan         # 当前查询方案（draft/locked/confirmed）


    # A1：Planner 输出的方案列表（单方案=长度1；多方案为 A2 并行入口）
    plans: list[QueryPlan]

    # A1：respond 分支输出给用户的文本（澄清/确认/最终回答）
    respond_text: str
