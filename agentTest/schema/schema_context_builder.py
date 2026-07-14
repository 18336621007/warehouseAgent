from agentTest.schema.column_retriever import ColumnRetriever
from agentTest.schema.table_retriever import TableRetriever


class SchemaContextBuilder:
    # 根据元数据获取候选表和候选字段，为 planner 提供稳定 schema 上下文
    def __init__(self, schema_tool):
        self.schema_tool = schema_tool
        self.table_retriever = TableRetriever()
        self.column_retriever = ColumnRetriever()

    def build(self, query: str):
        # 获取所有表，再基于问题召回候选表
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
