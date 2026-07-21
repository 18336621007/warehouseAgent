"""用增强元数据构建更丰富的 Schema Document，含别名、业务场景、维度标记"""
from typing import List
from langchain_core.documents import Document
from agentTest.metadata.mysql_store import load_enriched_tables


class EnrichedSchemaDocumentsBuilder:
    """从 MySQL 增强元数据构建 LangChain Document，信息密度远高于原始 schema"""

    def build_document(self, table_name: str, table_data: dict) -> Document:
        lines = [
            f"表名: {table_name}",
            f"所属领域: {table_data.get('domain', '')}",
            f"核心功能: {table_data.get('core_function', '')}",
            f"关键词: {', '.join(table_data.get('key_entities', []))}",
            f"适用场景: {', '.join(table_data.get('potential_use_cases', []))}",
            f"原始注释: {table_data.get('original_comment', '')}",
            "字段信息:",
        ]

        for col in table_data.get("columns", []):
            fields_type = col.get("fields_type", "dimension")
            type_tag = "【度量】" if fields_type == "measure" else "【维度】"
            aliases = "、".join(col.get("field_aliases", []))
            lines.append(
                f"  - {col['column_name']} {type_tag}"
                f"{' 别名: ' + aliases if aliases else ''}"
                f" 原始注释: {col.get('original_comment', '')}"
            )

        content = "\n".join(lines)

        return Document(
            page_content=content,
            metadata={
                "table_name": table_name,
                "domain": table_data.get("domain", ""),
            }
        )

    def build_documents(self) -> List[Document]:
        enriched = load_enriched_tables()
        documents = []
        for table_name, table_data in enriched.items():
            documents.append(self.build_document(table_name, table_data))
        return documents
