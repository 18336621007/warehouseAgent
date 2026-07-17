# SQL 执行节点，负责调用标准 SQL Tool 执行生成的 SQL。
from agentTest.langgraph_app.state.agent_state import AgentState
from agentTest.langchain_app.tools.tool_factory import build_tools


def execute_sql_node(state: AgentState):
    generated_sql = state.get("generated_sql", "")

    tools = build_tools()

    sql_query_tool = next(tool for tool in tools if tool.name == "sql_query")

    sql_result = sql_query_tool.invoke({
        "sql": generated_sql,
    })

    return {
        "sql_result": sql_result,
    }