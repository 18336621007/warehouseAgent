import copy


class SchemaVectorIndex:
    # SchemaVectorIndex 负责把 schema documents 转成可检索的向量索引。
    # 当前先做内存版索引，后续可替换为 FAISS / Milvus / pgvector。

    def __init__(self, embedder):
        self.embedder = embedder
        self._index_cache = None # 向量缓存
        self._build_index_count = 0  # 测试索引缓存是否命中

    def _build_document_text(self, document: dict) -> str:
        # 将 document 中适合检索的字段拼成统一文本。
        parts = [
            document.get("doc_id", ""),
            document.get("database_name", ""),
            document.get("table_name", ""),
            document.get("table_comment", ""),
            document.get("summary", ""),
            document.get("content", ""),
            " ".join(document.get("keywords", [])),
        ]
        return "\n".join(str(part or "") for part in parts)

    def build_index(self, documents: list[dict]) -> list[dict]:
        # 将 schema documents 构建为向量索引，缓存索引结果。现在是document粒度，后续升级为chunk粒度
        if self._index_cache is not None:
            return copy.deepcopy(self._index_cache)

        self._build_index_count += 1
        index_rows = []

        for document in documents:
            document_text = self._build_document_text(document) # 将document转成一段str
            vector = self.embedder.embed(document_text) # 将文本编码成 token -> weight 的稀疏向量
            index_rows.append(
                {
                    "doc_id": document.get("doc_id", ""),
                    "document": dict(document),
                    "vector": vector,
                }
            )

        self._index_cache = index_rows
        return copy.deepcopy(self._index_cache)

    def clear_cache(self):
        # 清空索引缓存，供测试或元数据刷新后重建。
        self._index_cache = None