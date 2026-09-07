# 简要注释：SQL 标准工具模块，负责把现有 SQL 查询能力包装成 LangChain StructuredTool。
from langchain_core.tools import StructuredTool

from agentTest.tools.sql_query_tool import SQLQueryTool


# 简要注释：创建标准 SQL 查询 Tool，复用现有 SQLQueryTool。
# 默认 timeout 180s，适配 Hive 多表 CROSS JOIN 独立聚合场景（用户实测 ~2 分钟）
DEFAULT_SQL_QUERY_TIMEOUT_SECONDS = 180


def build_sql_query_tool(datasource, query_timeout_seconds=DEFAULT_SQL_QUERY_TIMEOUT_SECONDS):
    sql_query_tool = SQLQueryTool(datasource, query_timeout_seconds=query_timeout_seconds)

    def run_sql_query(sql: str, partition_fields: list[str] | None = None):
        # partition_fields 可选：明细查询（无 pt_dt 分区）时透传方案时间字段，供执行守卫识别。
        args = {"sql": sql}
        if partition_fields:
            args["partition_fields"] = partition_fields
        return sql_query_tool.run(args)

    return StructuredTool.from_function(
        func=run_sql_query,
        name="sql_query",
        description="执行只读 Hive SQL 查询，自动进行 SQL 安全校验并返回查询结果",
    )