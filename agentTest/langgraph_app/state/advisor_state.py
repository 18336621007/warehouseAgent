# ── state/advisor.py ──
# Advisor 子图独立字段
from typing import TypedDict
from agentTest.langgraph_app.state.base_state import BaseState

class AdvisorState(BaseState, total=False):
    advisor_messages: list       # 多轮对话历史（Message 对象列表）
    confirmed_plan: dict         # Advisor 写入
    final_answer: str            # Advisor 回复文本