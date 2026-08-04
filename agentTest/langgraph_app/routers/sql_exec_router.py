# SQL 执行结果路由模块，根据 Hive 执行结果决定后续流程走向
from agentTest.langgraph_app.runtime.graph_logger import log_route_decision
from agentTest.langgraph_app.state.agent_state import AgentState

MAX_EXEC_RETRY = 2  # SQL 执行失败最大重试次数


def route_after_sql_execution(state: AgentState):
    """执行成功 → build_final_answer，失败未超限 → fix，失败超限 → 降级"""
    sql_exec_failed = state.get("sql_exec_failed", False)
    exec_retry_count = state.get("exec_retry_count", 0)

    if not sql_exec_failed:
        log_route_decision(
            "sql_exec_router",
            failed=False,
            decision="build_final_answer",
        )
        return "success"

    if exec_retry_count >= MAX_EXEC_RETRY:
        log_route_decision(
            "sql_exec_router",
            failed=True,
            exec_retry=exec_retry_count,
            decision="degrade",
        )
        return "degrade"

    log_route_decision(
        "sql_exec_router",
        failed=True,
        exec_retry=exec_retry_count,
        decision="retry",
    )
    return "retry"
