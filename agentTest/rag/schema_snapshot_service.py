"""
从SchemaTool或HiveMetadataProvider读取所有白名单表，对每张表调用SchemaDocumentBuilder
批量生成documents
"""
class SchemaSnapshotService:
    """负责批量生成 shcema 文档快照"""
    def __init__(self, schema_tool, document_builder):
        self.schema_tool = schema_tool
        self.document_builder = document_builder

    def build_snapshot(self):
        tables = self.schema_tool.list_tables()

        documents = []

        for table in tables:
            table_name = table.get("table_name", "")
            if not table_name:
                continue

            table_schema = self.schema_tool.describe_table(table_name)
            document = self.document_builder.build_table_document(table_schema)
            documents.append(document)

        return documents

    def build_snapshot_map(self):
        documents = self.build_snapshot()
        return {document["doc_id"]: document for document in documents}