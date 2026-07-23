# Supervisor 父图：调度 Planner → Seeker/Advisor 的多 Agent 架构入口
from langgraph.graph import StateGraph, START, END

from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.nodes.planner_node import build_planner_node
from agentTest.langgraph_app.graphs.seeker_graph import build_seeker_subgraph
from agentTest.langgraph_app.routers.planner_router import route_after_planner


# Advisor 占位节点，后续课程替换为完整子图
def advisor_stub_node(state: AgentState):
    return {"final_answer": "您的问题需要进一步澄清，此功能即将上线。"}


def build_supervisor_graph(runtime):
    # 父图使用同一个 AgentState（包含 planner 和 seeker 所有字段）
    supervisor = StateGraph(AgentState)

    # 注册 planner 节点（普通 Python 函数）
    supervisor.add_node("planner", build_planner_node(runtime))

    # 注册 seeker 子图（编译好的 StateGraph 直接作为节点）
    # LangGraph 自动对接子图的 START/END，同名 state 字段自动传递
    supervisor.add_node("seeker", build_seeker_subgraph(runtime))

    # 注册 advisor 占位（下节课替换为完整子图）
    supervisor.add_node("advisor", advisor_stub_node)

    # 设置边：START → planner → 路由 → seeker/advisor → END
    supervisor.add_edge(START, "planner")
    supervisor.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "seeker": "seeker",
            "advisor": "advisor",
        }
    )
    supervisor.add_edge("seeker", END)
    supervisor.add_edge("advisor", END)

    return supervisor.compile()