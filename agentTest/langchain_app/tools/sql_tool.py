# 简要注释：SQL 标准工具模块，负责把现有 SQL 查询能力包装成 LangChain StructuredTool。
from langchain_core.tools import StructuredTool

from agentTest.tools.sql_query_tool import SQLQueryTool


# 简要注释：创建标准 SQL 查询 Tool，复用现有 SQLQueryTool。
def build_sql_query_tool(datasource):
    sql_query_tool = SQLQueryTool(datasource)

    def run_sql_query(sql: str):
        return sql_query_tool.run({"sql": sql})

    return StructuredTool.from_function(
        func=run_sql_query,
        name="sql_query",
        description="执行只读 Hive SQL 查询，自动进行 SQL 安全校验并返回查询结果",
    )