# SQL 路由模块，负责根据 SQL 校验结果决定后续流程走向。
from agentTest.langgraph_app.runtime.graph_logger import log_route_decision
from agentTest.langgraph_app.state.agent_state import AgentState

MAX_SQL_RETRY_COUNT = 2 # 最大重试次数


# 根据 SQL 校验结果决定执行 SQL 还是进入修正分支。
def route_after_sql_validation(state: AgentState):
    sql_valid = state.get("sql_valid", False)
    retry_count = state.get("retry_count", 0)

    if sql_valid:
        # 打印执行分支日志
        log_route_decision(
            "route_after_sql_validation",
            sql_valid=sql_valid,
            retry_count=retry_count,
            decision="execute",
        )
        return "execute"

    if retry_count >= MAX_SQL_RETRY_COUNT:
        # 打印结束分支日志
        log_route_decision(
            "route_after_sql_validation",
            sql_valid=sql_valid,
            retry_count=retry_count,
            decision="end",
        )
        return "end"

    # 打印修正分支日志
    log_route_decision(
        "route_after_sql_validation",
        sql_valid=sql_valid,
        retry_count=retry_count,
        decision="fix",
    )
    return "fix"
