from agentTest.langgraph_app.runtime.graph_logger import log_route_decision
from agentTest.langgraph_app.state.agent_state import AgentState


def route_after_planner(state: AgentState):
    # 路由收敛两值：execute 进执行链，respond 直接结束等用户；
    # 执行回看后的 respond 携带 evaluator_pending，先走 Evaluator 评估再结束
    route = state.get("route") or "respond"
    planner_entities = state.get("planner_entities") or {}
    confirmed_plan = state.get("confirmed_plan") or {}

    if route == "respond" and state.get("evaluator_pending"):
        # 执行链落盘后 Planner 基于结果 respond：触发 Evaluator 评估本轮问答质量
        route = "evaluator"

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
