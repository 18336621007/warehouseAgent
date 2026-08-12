from agentTest.langgraph_app.runtime.graph_logger import log_route_decision
from agentTest.langgraph_app.state.agent_state import AgentState


def route_after_planner(state: AgentState):
    # Planner未返回合法路由时，默认进入Advisor继续澄清
    route = state.get("route") or "advisor"
    planner_entities = state.get("planner_entities") or {}
    confirmed_plan = state.get("confirmed_plan") or {}

    log_route_decision(
        "planner_router",
        decision=route,
        topic_status=state.get("topic_status", ""),
        completeness=planner_entities.get(
            "completeness",
            "",
        ),
        plan_status=confirmed_plan.get(
            "status",
            "",
        ),
        # draft 只是追问中的草稿，只有 locked/confirmed 才算已有完整方案
        has_confirmed_plan=confirmed_plan.get("status") in ("locked", "confirmed"),
    )

    return route