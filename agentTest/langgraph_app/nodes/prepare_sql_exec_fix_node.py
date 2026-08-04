# SQL 执行失败修正准备节点，把 Hive 报错信息整理成下一轮生成的输入
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.state.agent_state import AgentState


def prepare_sql_exec_fix_node(state: AgentState):
    """执行失败：把 Hive 错误信息写入 sql_fix_reason，供 generate_sql 重试"""
    exec_retry_count = state.get("exec_retry_count", 0)
    sql_exec_error = state.get("sql_exec_error", "SQL 执行失败")
    next_exec_retry = exec_retry_count + 1

    # 积累历史错误原因，避免修一个丢一个
    previous_fix_reason = state.get("sql_fix_reason", "")
    if previous_fix_reason:
        accumulated = previous_fix_reason + "; [Hive执行错误] " + sql_exec_error
    else:
        accumulated = "[Hive执行错误] " + sql_exec_error

    log_node_start(
        "prepare_sql_exec_fix",
        exec_retry=exec_retry_count,
        error=sql_exec_error[:200],
    )

    log_node_end(
        "prepare_sql_exec_fix",
        next_exec_retry=next_exec_retry,
    )

    return {
        "exec_retry_count": next_exec_retry,
        "sql_fix_reason": accumulated,
        "sql_exec_failed": False,
        # 重新进入 SQL 生成阶段
        "topic_status": "generating_sql",
    }
