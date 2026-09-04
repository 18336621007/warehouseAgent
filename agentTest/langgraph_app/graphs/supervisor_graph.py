# Supervisor 父图：调度 Planner → Seeker/Advisor 的多 Agent 架构入口
# Planner 是唯一路由者；Seeker 方案不可行时回 Planner 修复，机会耗尽给用户具体原因
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import AIMessage
from agentTest.langgraph_app.graphs.advisor_graph import build_advisor_subgraph
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.nodes.planner_node import build_planner_node
from agentTest.langgraph_app.graphs.seeker_graph import build_seeker_subgraph
from langgraph.checkpoint.memory import MemorySaver
from agentTest.langgraph_app.nodes.capture_user_message_node import capture_user_message_node
from agentTest.langgraph_app.routers.planner_router import route_after_planner
from agentTest.langgraph_app.routers.seeker_router import route_after_seeker


def plan_error_fallback_node(state):
    """Seeker 方案不可行且修复机会耗尽时，把具体失败原因转成给用户的最终答复。"""
    error = state.get("seeker_plan_error") or "当前查询无法安全执行"
    final_answer = "很抱歉，当前查询无法安全执行。\n\n" + error
    request_id = state.get("request_id", "")
    return {
        "final_answer": final_answer,
        "topic_status": "completed",
        "messages": [
            AIMessage(
                content=final_answer,
                name="seeker",
                id=f"{request_id}:seeker",
            )
        ],
    }



def build_supervisor_graph(runtime):
    # 父图使用同一个 AgentState（包含 planner 和 seeker 所有字段）
    supervisor = StateGraph(AgentState)

    # 统一记录本轮用户输入，再交给Planner判断路由
    supervisor.add_node("capture_user_message", capture_user_message_node)

    # 注册 planner 节点（普通 Python 函数）
    supervisor.add_node("planner", build_planner_node(runtime))

    # 注册 seeker 子图（编译好的 StateGraph 直接作为节点）
    # LangGraph 自动对接子图的 START/END，同名 state 字段自动传递
    supervisor.add_node("seeker", build_seeker_subgraph(runtime))

    # 注册 advisor 子图
    supervisor.add_node("advisor", build_advisor_subgraph(runtime))

    # 设置边：START先记录用户消息，再由Planner路由到Seeker或Advisor
    supervisor.add_edge(START, "capture_user_message")
    supervisor.add_edge("capture_user_message", "planner")
    supervisor.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "seeker": "seeker",
            "advisor": "advisor",
        }
    )
    # Seeker 方案不可行时回 Planner 修复；修复机会耗尽后给用户具体失败原因
    supervisor.add_node("plan_error_fallback", plan_error_fallback_node)
    supervisor.add_conditional_edges(
        "seeker",
        route_after_seeker,
        {
            "repair": "planner",
            "fallback": "plan_error_fallback",
            "end": END,
        },
    )
    supervisor.add_edge("plan_error_fallback", END)
    supervisor.add_edge("advisor", END)

    checkpointer = MemorySaver()
    return supervisor.compile(checkpointer=checkpointer)
