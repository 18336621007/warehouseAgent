# SQL 路由模块，负责根据 SQL 校验结果决定后续流程走向。
from agentTest.langgraph_app.state.agent_state import AgentState

MAX_SQL_RETRY_COUNT = 2 #最大重试次数


# 根据 SQL 校验结果决定执行 SQL 还是进入修正分支。
def route_after_sql_validation(state: AgentState):
    if state.get("sql_valid", False):
        return "execute"

    retry_count = state.get("retry_count", 0)
    if retry_count >= MAX_SQL_RETRY_COUNT:
        return "end"
    return "fix"