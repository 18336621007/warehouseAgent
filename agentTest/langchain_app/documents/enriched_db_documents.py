"""库级增强元数据 → LangChain Document，用于 FAISS 三层索引中的库层检索"""
from typing import List
from langchain_core.documents import Document
from agentTest.metadata.mysql_store import load_enriched_databases


class EnrichedDatabaseDocumentsBuilder:

    def build_document(self, database_name: str, db_data: dict) -> Document:
        # 拼成一段自然语言文本，LLM 检索时通过语义匹配
        lines = [
            f"库名: {database_name}",
            f"领域: {db_data.get('domain', '')}",
            f"描述: {db_data.get('description', '')}",
            f"包含表数: {len(db_data.get('full_table_list', []))} 张",
        ]

        content = "\n".join(lines)

        return Document(
            page_content=content,
            metadata={
                "database": database_name,
                "domain": db_data.get("domain", ""),
                "table_count": len(db_data.get("full_table_list", [])),
            }
        )

    def build_documents(self) -> List[Document]:
        databases = load_enriched_databases()
        documents = []
        for db_name, db_data in databases.items():
            documents.append(self.build_document(db_name, db_data))
        return documents