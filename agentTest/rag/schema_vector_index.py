import copy


class SchemaVectorIndex:
    # SchemaVectorIndex 负责将 schema documents 转为分字段向量索引，并提供缓存。

    def __init__(self, embedder, embedding_cache=None):
        self.embedder = embedder
        self.embedding_cache = embedding_cache # 持久化 embedding 缓存 跨进程/重启可用
        self._index_cache = None  # 内存级向量索引缓存 存的是本轮已经构建好的整份索引 仅当前进程内复用
        self._build_index_count = 0  # 统计索引是否命中缓存

    def _embed_field(self, document: dict, field_name: str, text: str) -> dict[str, float]:
        # 优先使用持久化 embedding 缓存，未命中时再调用 embedder。
        if self.embedding_cache is None:
            return self.embedder.embed(text)


        cached_vector = self.embedding_cache.get_field(document, field_name)
        if cached_vector is not None:
            return dict(cached_vector)

        vector = self.embedder.embed(text)
        self.embedding_cache.set_field(document, field_name, vector)
        return dict(vector)



    def _build_column_names_text(self, document: dict) -> str:
        # 提取所有列名，供字段级检索使用。
        columns = document.get("columns", [])
        return " ".join(
            str(column.get("column_name", "") or "")
            for column in columns
        )

    def build_index(self, documents: list[dict]) -> list[dict]:
        # 将 documents 构建为分字段向量索引，并缓存索引结果。
        if self._index_cache is not None:
            return copy.deepcopy(self._index_cache)

        self._build_index_count += 1
        index_rows = []

        for document in documents:
            table_name_text = str(document.get("table_name", "") or "")
            table_comment_text = str(document.get("table_comment", "") or "")
            summary_text = str(document.get("summary", "") or "")
            keywords_text = " ".join(document.get("keywords", []))
            content_text = str(document.get("content", "") or "")
            column_names_text = self._build_column_names_text(document)

            index_rows.append(
                {
                    "doc_id": document.get("doc_id", ""),
                    "document": dict(document),
                    "table_name_vector": self._embed_field(document, "table_name", table_name_text),
                    "table_comment_vector": self._embed_field(document, "table_comment", table_comment_text),
                    "summary_vector": self._embed_field(document, "summary", summary_text),
                    "keywords_vector": self._embed_field(document, "keywords", keywords_text),
                    "content_vector": self._embed_field(document, "content", content_text),
                    "column_names_vector": self._embed_field(document, "column_names", column_names_text),
                }
            )

        self._index_cache = index_rows
        return copy.deepcopy(self._index_cache)

    def clear_cache(self):
        # 清空索引缓存，供测试或元数据刷新后重建。
        self._index_cache = None
