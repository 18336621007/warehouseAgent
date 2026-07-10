
from agentTest.schema.column_retriever import ColumnRetriever
from agentTest.schema.table_retriever import TableRetriever
from agentTest.tools.schema_tool import SchemaTool

"""
{
    "query": query,
    "candidate_tables": {
        "table_name": ...,
        "table_comment": ...,
        "score": ...,
        "candidate_columns": {
            "name": ...,
            "type": ...,
            "comment": ...,
            "score": ...,
        }
    },
}
"""
class SchemaContextBuilder:
    """根据元数据获取候选表/列"""
    def __init__(self, schema_tool):
        self.schema_tool = schema_tool
        self.table_retriever = TableRetriever()
        self.column_retriever = ColumnRetriever()


    def build(self, query: str):
        #获取所有表
        tables = self.schema_tool.list_tables()
        #获取候选表
        candidate_tables = self.table_retriever.retrieve(query, tables)

        result_tables = []

        for table in candidate_tables:
            table_name = table.get("table_name")
            #获取表具体列
            table_schema = self.schema_tool.describe_table(table_name)
            # 获取候选列
            candidate_columns = self.column_retriever.retrieve(query, table_schema)
            result_tables.append({
                "table_name": table_name,
                "table_comment": table.get("table_comment", ""),
                "score": table.get("score", 0),
                "candidate_columns": candidate_columns,
            })

        return {
            "query": query,
            "candidate_tables": result_tables,
        }



