from agentTest.langgraph_app.runtime.graph_logger import log_route_decision
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langgraph_app.services.result_store import resolve_result


def route_after_planner(state: AgentState):
    # Planner未返回合法路由时，默认进入Advisor继续澄清
    route = state.get("route") or "advisor"
    planner_entities = state.get("planner_entities") or {}
    confirmed_plan = state.get("confirmed_plan") or {}
    # result_review 兜底：用户引用结果但程序无法在索引中定位到对应轮次时，降级 Advisor 澄清
    if route == "result_review":
        ref = planner_entities.get("result_ref") or ""
        if not resolve_result(str(state.get("conversation_id") or ""), str(ref or "")):
            route = "advisor"

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
        # 方案不区分草稿/提交：有表即视为已有共享方案
        has_confirmed_plan=bool(confirmed_plan.get("table") or confirmed_plan.get("tables")),
    )

    return route