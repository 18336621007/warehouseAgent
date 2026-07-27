# ── state/agent_state.py ──
# 父图 AgentState：继承所有子图 State，Supervisor 使用
from typing import TypedDict
from agentTest.langgraph_app.state.planner_state import PlannerState
from agentTest.langgraph_app.state.advisor_state import AdvisorState
from agentTest.langgraph_app.state.seeker_state import SeekerState

class AgentState(PlannerState, AdvisorState, SeekerState, total=False):
    pass