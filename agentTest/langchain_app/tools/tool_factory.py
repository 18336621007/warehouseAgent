
from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.langchain_app.tools.schema_tool import build_list_tables_tool, build_describe_table_tool
from agentTest.langchain_app.tools.sql_tool import build_sql_query_tool
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider


# 简要注释：Tool 工厂模块，负责统一初始化并返回标准 LangChain tools 列表。
def build_tools():
    meta_provider = HiveMetadataProvider()
    datasource = HiveDataSource()

    list_tables_tool = build_list_tables_tool(meta_provider)
    describe_table_tool = build_describe_table_tool(meta_provider)
    sql_query_tool = build_sql_query_tool(datasource)

    return [
        list_tables_tool,
        describe_table_tool,
        sql_query_tool
    ]