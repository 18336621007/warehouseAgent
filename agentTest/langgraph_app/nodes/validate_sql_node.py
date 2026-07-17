# 作用：校验 Hive SQL 是否满足安全规则
# 输入：SQL 字符串
# 输出：(bool, str)
# 第一个值表示是否通过
# 第二个值表示提示信息
from agentTest.langgraph_app.runtime.graph_logger import log_node_event
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.validate.sql_validate import validate_hive_sql


def validate_sql_node(state: AgentState):
    generated_sql = state.get("generated_sql", "")
    log_node_event("validate_sql", f"开始校验SQL，sql={generated_sql}")
    is_valid, msg = validate_hive_sql(generated_sql)
    log_node_event("validate_sql", f"校验完成，is_valid={is_valid}, msg={msg}")
    return {
        "sql_valid": is_valid,
        "sql_error": "" if is_valid else msg,
    }

