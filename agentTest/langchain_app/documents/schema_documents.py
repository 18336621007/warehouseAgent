# 简要注释：Schema 文档构建模块，负责把元数据转换为标准文档对象。
from typing import List, Dict, Any

from langchain_core.documents import Document
from agentTest.db.hive_guardrails import ALLOWED_TABLES
"""
Document
它本质上就是一个“文档对象”，一般至少有两个核心字段：
page_content：真正给检索和模型看的文本
metadata：附加信息，不一定直接给模型看，但方便过滤、定位、调试
举例
Document(
    page_content="表名: ods_order\n表说明: 订单表\n字段信息:\n- order_id | string | 订单id",
    metadata={
        "table_name": "ods_order",
        "database_name": "dwd"
    }
)
"""

class SchemaDocumentsBuilder:
    # 初始化文档构建器，注入现有元数据提供器
    def __init__(self, meta_provider):
        self.meta_provider = meta_provider


    #构建单个表的文档内容，返回文本
    def build_table_content(self, table_schema: dict) -> str:
        table_name = table_schema.get('table_name', "")
        table_comment = table_schema.get('table_comment', "")
        columns = table_schema.get('columns', [])

        lines = [
            f"表名: {table_name}",
            f"表说明: {table_comment}",
            "字段信息:"
        ]

        for column in columns:
            column_name = column.get('name', "")
            column_type = column.get('type', "")
            column_comment = column.get('comment', "")
            lines.append(f" - {column_name} | {column_type} | {column_comment}")

        return "\n".join(lines)

    #把表结构转换为标准文档对象，返回Document对象
    def build_document(self, table_schema: dict) -> Document:
        content = self.build_table_content(table_schema)
        metadata = {
            "database_name": table_schema.get("database_name", ""),
            "table_name": table_schema.get("table_name", ""),
            "table_type": table_schema.get("table_type", ""),

        }
        return Document(
            page_content=content,
            metadata=metadata
        )




    # 复用现有 list_tables 和 describe_table，批量构建 schema 文档
    def build_documents(self) -> List[Document]:
        documents = []
        tables = self.meta_provider.list_tables()

        for table in tables:
            table_name = table.get("table_name", "")
            if not table_name:
                continue

            table_schema = self.meta_provider.describe_table(table_name)
            document = self.build_document(table_schema)
            documents.append(document)

        return documents