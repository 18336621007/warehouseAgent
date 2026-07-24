"""字段级增强元数据 → LangChain Document，用于 FAISS 三层索引中的字段层检索"""
from typing import List
from langchain_core.documents import Document
from agentTest.metadata.mysql_store import load_enriched_columns


class EnrichedColumnDocumentsBuilder:

    def build_document(self, col: dict) -> Document:
        fields_type = col.get("fields_type", "dimension")
        type_tag = "【度量】" if fields_type == "measure" else "【维度】"

        aliases = "、".join(col.get("field_aliases", []))
        samples = "、".join(col.get("sample_values", []))

        lines = [f"字段: {col['database_name']}.{col['table_name']}.{col['column_name']}"]
        lines.append(f"类型: {type_tag}")
        if aliases:
            lines.append(f"别名: {aliases}")
        if samples:
            lines.append(f"采样值: {samples}")
        if col.get("original_comment"):
            lines.append(f"原始备注: {col['original_comment']}")

        content = "\n".join(lines)

        return Document(
            page_content=content,
            metadata={
                "database": col.get("database_name", ""),
                "table": f"{col.get('database_name', '')}.{col.get('table_name', '')}",
                "column": col.get("column_name", ""),
                "fields_type": fields_type,
            }
        )

    def build_documents(self) -> List[Document]:
        columns = load_enriched_columns()
        documents = []
        for col in columns:
            documents.append(self.build_document(col))
        return documents