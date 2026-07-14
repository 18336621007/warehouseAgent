# SchemaSnapshotService 负责批量生成 schema 文档快照。
# 它会从 SchemaTool 获取当前可见表列表，再逐表读取表结构，
# 调用 SchemaDocumentBuilder 转成统一的 schema documents，
# 供后续 schema RAG 检索使用。
import copy


class SchemaSnapshotService:
    """负责批量生成 shcema 文档快照"""
    def __init__(self, schema_tool, document_builder):
        self.schema_tool = schema_tool
        self.document_builder = document_builder
        #缓存
        self._snapshot_cache = None
        self._build_snapshot_count = 0 #测试缓存

    def build_snapshot(self):
        # 批量构建 schema documents：
        # 1. 获取当前可见表列表
        # 2. 逐表读取表结构
        # 3. 转换为 schema document
        # 4. 返回当前轮可检索的 schema 文档集合

        #缓存命中直接返回
        if self._snapshot_cache is not None:
            return list(self._snapshot_cache)

        self._build_snapshot_count += 1
        tables = self.schema_tool.list_tables()

        documents = []

        for table in tables:
            table_name = table.get("table_name", "")
            if not table_name:
                continue

            table_schema = self.schema_tool.describe_table(table_name)
            document = self.document_builder.build_table_document(table_schema)
            documents.append(document)

        self._snapshot_cache = documents
        return  copy.deepcopy(self._snapshot_cache)

    def build_snapshot_map(self):
        # 将 schema documents 按 doc_id 转成映射，便于后续快速按文档 ID 查询。
        documents = self.build_snapshot()
        return {document["doc_id"]: document for document in documents}

    def clear_snapshot_cache(self):
        self._snapshot_cache = None