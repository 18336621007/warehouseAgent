# SQL 修正准备节点，负责把校验失败原因整理成下一轮生成的输入。
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.state.agent_state import AgentState


def prepare_sql_fix_node(state: AgentState):
    retry_count = state.get("retry_count", 0)
    sql_error = state.get("sql_error", "SQL校验失败")
    next_retry_count = retry_count + 1

    # 打印节点开始日志
    log_node_start(
        "prepare_sql_fix",
        retry_count=retry_count,
        sql_error=sql_error,
    )

    # 打印节点结束日志
    log_node_end(
        "prepare_sql_fix",
        next_retry_count=next_retry_count,
        sql_fix_reason=sql_error,
    )

    return {
        "retry_count": next_retry_count,
        "sql_fix_reason": sql_error
    }
