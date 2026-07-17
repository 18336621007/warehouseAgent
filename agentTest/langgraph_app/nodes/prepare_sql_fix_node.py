# SQL 修正准备节点，负责把校验失败原因整理成下一轮生成的输入。
from agentTest.langgraph_app.state.agent_state import AgentState


def prepare_sql_fix_node(state: AgentState):
    retry_count = state.get("retry_count", 0)
    sql_errors = state.get("sql_errors", "SQL校验失败")

    return {
        "retry_count": retry_count,
        "sql_errors": sql_errors
    }