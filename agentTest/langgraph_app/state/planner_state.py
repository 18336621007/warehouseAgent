# ── state/planner.py ──
# Planner 独有字段 + 跨模块只读字段
from agentTest.langgraph_app.state.base_state import BaseState
from agentTest.langgraph_app.state.planner_handoff_state import PlannerHandoffState
from agentTest.langgraph_app.state.query_plan import QueryPlan

class PlannerState(BaseState, PlannerHandoffState, total=False):
    route: str                   # respond（单 Agent 收敛为单一终态）

    # Planner 是唯一决策者：直接给用户输出文本（澄清/确认/最终回答）
    confirmed_plan: QueryPlan         # 当前查询方案（draft/locked/confirmed）

    # respond 分支输出给用户的文本（澄清/确认/最终回答）
    respond_text: str
