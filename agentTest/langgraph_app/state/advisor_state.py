# ── state/advisor.py ──
# Advisor 子图独立字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState
from agentTest.langgraph_app.state.planner_handoff_state import PlannerHandoffState
from agentTest.langgraph_app.state.query_plan import QueryPlan

class AdvisorState(BaseState, PlannerHandoffState, total=False):
    # Advisor 负责生成 status=locked 的标准查询方案
    confirmed_plan: QueryPlan         # 当前查询方案（draft/locked/confirmed），Advisor 写入
    final_answer: str            # Advisor 回复文本
    advisor_thinking: list[str]  # Advisor ReAct 每步 LLM 输出，供前端思考过程展示
