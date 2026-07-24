"""表级增强元数据 → LangChain Document，用于 FAISS 三层索引中的表层检索

   与 enriched_schema_documents.py 的区别：
   - 那个是单层索引（表+字段混一起），这个是三层索引中的表层
   - 每张表一个独立 Document，字段信息在另一个 column 层单独建索引
"""
from typing import List
from langchain_core.documents import Document
from agentTest.metadata.mysql_store import load_enriched_tables


class EnrichedTableDocumentsBuilder:

    def build_document(self, table_name: str, table_data: dict) -> Document:
        lines = [
            f"表名: {table_name}",
            f"所属领域: {table_data.get('domain', '')}",
            f"核心功能: {table_data.get('core_function', '')}",
            f"关键词: {', '.join(table_data.get('key_entities', []))}",
            f"适用场景: {', '.join(table_data.get('potential_use_cases', []))}",
        ]

        # 字段名列表（不含详细信息，那是 column 层的事）
        column_names = [col.get("column_name", "") for col in table_data.get("columns", [])]
        if column_names:
            lines.append(f"包含字段: {', '.join(column_names)}")

        content = "\n".join(lines)

        return Document(
            page_content=content,
            metadata={
                "database": table_name.split(".")[0] if "." in table_name else "",
                "table": table_name,
                "domain": table_data.get("domain", ""),
            }
        )

    def build_documents(self) -> List[Document]:
        tables = load_enriched_tables()
        documents = []
        for table_name, table_data in tables.items():
            documents.append(self.build_document(table_name, table_data))
        return documents