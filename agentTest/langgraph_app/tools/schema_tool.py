from langchain_core.tools import StructuredTool
# 简要注释：Schema 工具，复用现有元数据查询能力并统一工具入口。

# 创建列出表列表的标准Tool
def build_list_tables_tool(meta_provider):
    def list_tables():
        return meta_provider.list_tables()

    return StructuredTool.from_function(
        func=list_tables,
        name="list_tables",
        description="查询当前Hive数据库下可访问的表列表",
    )

#创建查询单表结构的标准Tool

def build_describe_table_tool(meta_provider):
    def describe_table(table_name: str):
        return meta_provider.describe_table(table_name)

    return StructuredTool.from_function(
        func=describe_table,
        name="describe_table",
        description="查询指定Hive表的字段结构信息"
    )