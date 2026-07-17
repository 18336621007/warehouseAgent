# SQL 执行节点，负责调用标准 SQL Tool 执行生成的 SQL。
from agentTest.langgraph_app.runtime.graph_logger import log_node_end
from agentTest.langgraph_app.runtime.graph_logger import log_node_start
from agentTest.langgraph_app.state.agent_state import AgentState


def build_execute_sql_node(runtime):

    tools = runtime["tools"]
    sql_query_tool = next(tool for tool in tools if tool.name == "sql_query")

    def execute_sql_node(state: AgentState):

        generated_sql = state.get("generated_sql", "")

        # 打印节点开始日志
        log_node_start("execute_sql", generated_sql=generated_sql)

        sql_result = sql_query_tool.invoke({
            "sql": generated_sql
        })
        row_count = sql_result.get("row_count", 0) if isinstance(sql_result, dict) else 0

        # 打印节点结束日志
        log_node_end("execute_sql", row_count=row_count)

        return {
            "sql_result": sql_result,
        }

    return execute_sql_node
