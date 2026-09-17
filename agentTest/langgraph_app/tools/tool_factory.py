
from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.datasource.doris_datasource import DorisDataSource
from agentTest.datasource.trino_datasource import TrinoDataSource
from agentTest.langgraph_app.tools.schema_tool import build_list_tables_tool, build_describe_table_tool
from agentTest.langgraph_app.tools.sql_tool import build_sql_query_tool
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider


# 简要注释：Tool 工厂模块，负责统一初始化并返回标准 LangChain tools 列表。
def build_tools(meta_provider=None):
    # Runtime 可以注入共享 Provider，避免重复创建元数据实例
    if meta_provider is None:
        meta_provider = HiveMetadataProvider()
    # 全部查询引擎：Trino 主通道、Hive 兜底、Doris 专属(data_project)

    list_tables_tool = build_list_tables_tool(meta_provider)
    describe_table_tool = build_describe_table_tool(meta_provider)
    sql_query_tools = [
        build_sql_query_tool(datasource_cls(), engine_name=engine_name)
        for engine_name, datasource_cls in (
            ("trino", TrinoDataSource),
            ("hive", HiveDataSource),
            ("doris", DorisDataSource),
        )
    ]

    return [
        list_tables_tool,
        describe_table_tool,
        *sql_query_tools,
    ]