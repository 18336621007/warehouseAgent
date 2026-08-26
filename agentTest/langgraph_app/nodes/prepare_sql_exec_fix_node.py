# SQL 执行失败修正准备节点，把 Hive 报错信息整理成下一轮生成的输入
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
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

    # ── 新增：把上次一致性校验通过（但 Hive 执行失败）的 SQL 注入 hint，帮助 LLM 收敛 ──
    pass_history = state.get("sql_pass_history") or []
    last_pass_sql = state.get("generated_sql", "")
    pass_hint = ""
    # 历史里如果存在与当前 generated_sql 不同的 PASS 写法，说明当前 SQL 不是最优，让 LLM 沿用历史写法
    prior_pass = [s for s in pass_history if s and s != last_pass_sql]
    if prior_pass:
        pass_hint = (
            "\n\n参考：之前一致性校验已通过的 SQL 写法（请沿用或仅做最小修改，避免反复重写）：\n"
            + "\n---\n".join(prior_pass[-1:])
        )
        accumulated = accumulated + pass_hint
        log_node_event(
            "prepare_sql_exec_fix",
            f"注入上次 PASS SQL 作为参考（共 {len(prior_pass)} 条历史）",
        )

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
