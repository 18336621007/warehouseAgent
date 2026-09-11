# ── state/agent_state.py ──
# 父图 AgentState：继承 Planner + 执行链（原 Seeker）State，Supervisor 使用
# A1：Advisor 已并入 Planner，不再继承 AdvisorState
from typing import TypedDict, Any
from agentTest.langgraph_app.state.planner_state import PlannerState
from agentTest.langgraph_app.state.seeker_state import SeekerState


class GraphInput(TypedDict):
    # Web层只允许传入本次执行需要的身份字段和用户输入
    conversation_id: str
    topic_id: str
    request_id: str
    current_user_input: str


class GraphOutput(TypedDict, total=False):
    # 输出层只暴露前端展示和持久化需要的字段
    topic_id: str
    topic_status: str
    route: str
    final_answer: str
    generated_sql: str
    result_preview: list[Any]
    evaluator_score: float
    evaluator_dialogue_id: int

class AgentState(PlannerState, SeekerState, total=False):
    pass