from agentTest.schema.column_retriever import ColumnRetriever
from agentTest.schema.table_retriever import TableRetriever
# SchemaContextBuilder 负责根据用户问题构建结构化 schema 上下文。
# 它会先从 SchemaTool 获取当前可见表的原始元数据，再通过表召回和字段召回，
# 将全量元数据压缩成当前 query 最相关的候选表/字段集合，
# 供 planner 在生成步骤时优先参考。

class SchemaContextBuilder:
    # 根据元数据获取候选表和候选字段，为 planner 提供稳定 schema 上下文
    def __init__(self, schema_tool):
        self.schema_tool = schema_tool
        self.table_retriever = TableRetriever()
        self.column_retriever = ColumnRetriever()

    def build(self, query: str):
        # 根据 query 构建结构化 schema_context：
        # 1. 获取当前可见表列表
        # 2. 召回最相关的候选表
        # 3. 获取候选表结构并召回候选字段
        # 4. 返回供 planner 使用的结构化 schema 上下文
        tables = self.schema_tool.list_tables()
        candidate_tables = self.table_retriever.retrieve(query, tables)

        result_tables = []
        for table in candidate_tables:
            table_name = table.get("table_name")
            table_schema = self.schema_tool.describe_table(table_name)
            candidate_columns = self.column_retriever.retrieve(query, table_schema)

            result_tables.append({
                "database_name": table.get("database_name", table_schema.get("database_name", "")),
                "table_name": table_name,
                "table_comment": table.get("table_comment", table_schema.get("table_comment", "")),
                "table_type": table.get("table_type", table_schema.get("table_type", "")),
                "score": table.get("score", 0),
                "candidate_columns": candidate_columns,
            })

        return {
            "query": query,
            "candidate_tables": result_tables,
        }
