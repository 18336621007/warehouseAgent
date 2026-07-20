# 作用：校验 Hive SQL 是否满足安全规则
# 输入：SQL 字符串
# 输出：(bool, str)
# 第一个值表示是否通过
# 第二个值表示提示信息
from agentTest.db.hive_guardrails import validate_sql_with_guardrails
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.validate.sql_validate import validate_hive_sql


def validate_sql_node(state: AgentState):
    generated_sql = state.get("generated_sql", "")

    # 打印节点开始日志
    log_node_start("validate_sql", generated_sql=generated_sql)

    # 基础合法性校验
    is_valid, message = validate_hive_sql(generated_sql)
    if not is_valid:
        return {
            "sql_valid": False,
            "sql_error": message,
        }
    # 资源保护校验
    is_valid, message = validate_sql_with_guardrails(generated_sql)
    if not is_valid:
        return {
            "sql_valid": False,
            "sql_error": message,
        }

    # 打印节点结束日志
    log_node_end("validate_sql", sql_valid=is_valid, sql_error=message)

    return {
        "sql_valid": True,
        "sql_error": "",
    }
